# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Trainer for PPO training."""

from __future__ import annotations

import argparse
import copy
import itertools
import os
import sys
from datetime import datetime
from typing import Any

import deepspeed
import torch
import torch.distributed as dist
import torch.nn as nn
from deepspeed.ops.adam import FusedAdam
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from transformers import CONFIG_NAME, GenerationConfig, PreTrainedModel, get_scheduler
from transformers.integrations.deepspeed import HfDeepSpeedConfig

from align_anything.datasets import (
    DummyDataset,
    PromptOnlyBatch,
    PromptOnlyDataset,
    SupervisedDataset,
)
from align_anything.models.pretrained_model import load_pretrained_models
from align_anything.models.pretrained_model_with_value import load_pretrained_model_with_value_head
from align_anything.utils.logger import Logger
from align_anything.utils.multi_process import (
    get_all_reduce_max,
    get_all_reduce_mean,
    get_current_device,
    is_main_process,
)
from align_anything.utils.tools import (
    batch_retokenize,
    custom_cfgs_to_dict,
    dict_to_namedtuple,
    gather_log_probabilities,
    get_optimizer_grouped_parameters,
    is_same_processor,
    is_same_tokenizer,
    masked_mean,
    namedtuple_to_dict,
    prepare_ds_eval_cfgs,
    prepare_ds_train_cfgs,
    read_cfgs,
    seed_everything,
    update_dict,
)


class PPOTrainer:  # pylint: disable=too-many-instance-attributes
    """Trainer base class for PPO training."""

    def __init__(self, cfgs, ds_cfgs) -> None:
        """Initialize trainer."""
        self.cfgs = cfgs
        self.ds_train_cfgs = prepare_ds_train_cfgs(custom_cfgs=cfgs.train_cfgs, raw_ds_cfgs=ds_cfgs)
        self.ds_eval_cfgs = prepare_ds_eval_cfgs(custom_cfgs=cfgs.train_cfgs, raw_ds_cfgs=ds_cfgs)
        self.global_step = 0

        self.init_check()
        dist.barrier()
        self.init_models()
        dist.barrier()
        self.init_datasets()
        dist.barrier()
        self.init_engines()
        dist.barrier()
        self.init_logger()

        self.kl_coeff = self.cfgs.train_cfgs.kl_coeff
        self.clip_range_ratio = self.cfgs.train_cfgs.clip_range_ratio
        self.clip_range_score = self.cfgs.train_cfgs.clip_range_score
        self.clip_range_value = self.cfgs.train_cfgs.clip_range_value
        self.ptx_coeff = self.cfgs.train_cfgs.ptx_coeff
        self.gamma = self.cfgs.train_cfgs.gamma
        self.gae_lambda = self.cfgs.train_cfgs.gae_lambda

    def init_logger(self) -> None:
        """Set logger."""
        logger_cfgs = self.cfgs.logger_cfgs
        time = datetime.now().strftime('%Y-%m-%d-%H-%M-%S')

        self.logger = Logger(
            log_type=logger_cfgs.log_type,
            log_dir=logger_cfgs.output_dir,
            log_project=logger_cfgs.log_project,
            log_run_name=f'{logger_cfgs.log_run_name}-{self.cfgs.data_cfgs.train_datasets}-{time}',
            config=namedtuple_to_dict(self.cfgs),
        )

    def init_models(self) -> None:
        """Initialize model and tokenizer."""
        if self.ds_train_cfgs['zero_optimization']['stage'] == 3:
            self.dstchf_train = HfDeepSpeedConfig(self.ds_train_cfgs)
        if self.ds_eval_cfgs['zero_optimization']['stage'] == 3:
            self.dsechf_eval = HfDeepSpeedConfig(self.ds_eval_cfgs)
        # loading actor model
        self.actor_model, self.tokenizer, self.processor = load_pretrained_models(
            self.cfgs.model_cfgs.actor_model_name_or_path,
            model_max_length=self.cfgs.model_cfgs.model_max_length,
            padding_side='left',
            trust_remote_code=self.cfgs.model_cfgs.trust_remote_code,
            freeze_mm_proj=self.cfgs.train_cfgs.freeze_mm_proj,
            freeze_vision_tower=self.cfgs.train_cfgs.freeze_vision_tower,
        )
        # loading actor reference model
        self.actor_reference_model, _, _ = load_pretrained_models(
            self.cfgs.model_cfgs.actor_model_name_or_path,
            model_max_length=self.cfgs.model_cfgs.model_max_length,
            padding_side='left',
            trust_remote_code=self.cfgs.model_cfgs.trust_remote_code,
        )
        # loading reward model
        self.reward_model, self.reward_tokenizer, self.reward_processor = (
            load_pretrained_model_with_value_head(
                self.cfgs.model_cfgs.reward_model_name_or_path,
                model_max_length=self.cfgs.model_cfgs.model_max_length,
                padding_side='right',
                trust_remote_code=self.cfgs.model_cfgs.trust_remote_code,
            )
        )
        # loading reward critic model
        self.reward_critic_model, self.reward_critic_tokenizer, self.reward_critic_processor = (
            load_pretrained_model_with_value_head(
                self.cfgs.model_cfgs.reward_critic_model_name_or_path,
                model_max_length=self.cfgs.model_cfgs.model_max_length,
                padding_side='left',
                trust_remote_code=self.cfgs.model_cfgs.trust_remote_code,
            )
        )
        # initial checking
        if is_same_tokenizer(self.tokenizer, self.reward_tokenizer):
            self.reward_tokenizer = self.tokenizer
        if not is_same_tokenizer(self.tokenizer, self.reward_critic_tokenizer):
            raise ValueError(
                (
                    'Reward critic tokenizer must be the same as actor tokenizer. '
                    'Expected {0.__module__}.{0.__qualname__}(vocab_size={1}), '
                    'but got {2.__module__}.{2.__qualname__}(vocab_size={3}). '
                    'Please consider pass `--reward_critic_model_name_or_path` from the command line.'
                ).format(
                    type(self.tokenizer),
                    len(self.tokenizer),
                    type(self.reward_critic_tokenizer),
                    len(self.reward_critic_tokenizer),
                ),
            )

        if is_same_processor(self.processor, self.reward_processor):
            self.reward_processor = self.processor
        # training setup
        self.reward_critic_tokenizer = self.tokenizer
        self.reward_critic_processor = self.processor

        self.generation_config = GenerationConfig(
            max_length=self.cfgs.model_cfgs.model_max_length,
            temperature=self.cfgs.model_cfgs.temperature,
            top_p=self.cfgs.model_cfgs.top_p,
            repetition_penalty=self.cfgs.model_cfgs.repetition_penalty,
            do_sample=True,
            bos_token_id=self.tokenizer.bos_token_id,
            eos_token_id=self.tokenizer.eos_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
        )

    def init_check(self) -> None:
        """Initial configuration checking."""
        if (
            self.cfgs.train_cfgs.per_device_prompt_batch_size
            % self.cfgs.train_cfgs.per_device_train_batch_size
            != 0
        ):
            raise ValueError(
                'The number of prompt-only samples must be divisible by the micro batch size.',
            )

    def init_datasets(self) -> None:
        """Initialize training and evaluation datasets."""
        # load training datasets
        prompt_only_dataset = PromptOnlyDataset(
            path=self.cfgs.data_cfgs.train_datasets,
            template=self.cfgs.data_cfgs.train_template,
            tokenizer=self.tokenizer,
            processor=self.processor,
            size=self.cfgs.data_cfgs.train_size,
            split=self.cfgs.data_cfgs.train_split,
            subset=self.cfgs.data_cfgs.train_subset,
            data_files=self.cfgs.data_cfgs.train_data_files,
        )
        self.prompt_only_dataloader = DataLoader(
            prompt_only_dataset,
            collate_fn=prompt_only_dataset.get_collator(),
            sampler=DistributedSampler(prompt_only_dataset, shuffle=True),
            batch_size=self.cfgs.train_cfgs.per_device_prompt_batch_size,
        )
        # load evaluation datasets
        if self.cfgs.data_cfgs.eval_datasets:
            eval_dataset = PromptOnlyDataset(
                path=self.cfgs.data_cfgs.eval_datasets,
                template=self.cfgs.data_cfgs.eval_template,
                tokenizer=self.tokenizer,
                processor=self.processor,
                size=self.cfgs.data_cfgs.eval_size,
                split=self.cfgs.data_cfgs.eval_split,
                subset=self.cfgs.data_cfgs.eval_subset,
                data_files=self.cfgs.data_cfgs.eval_data_files,
            )
            self.eval_dataloader = DataLoader(
                eval_dataset,
                collate_fn=eval_dataset.get_collator(),
                sampler=DistributedSampler(eval_dataset, shuffle=True),
                batch_size=self.cfgs.train_cfgs.per_device_eval_batch_size,
            )
        else:
            self.eval_dataloader = None
        # load ptx datasets
        self.use_ptx = self.cfgs.data_cfgs.ptx_datasets is not None
        if self.use_ptx:
            ptx_dataset = SupervisedDataset(
                path=self.cfgs.data_cfgs.ptx_datasets,
                template=self.cfgs.data_cfgs.ptx_template,
                tokenizer=self.tokenizer,
                processor=self.processor,
                size=self.cfgs.data_cfgs.ptx_size,
                split=self.cfgs.data_cfgs.ptx_split,
                subset=self.cfgs.data_cfgs.ptx_subset,
                data_files=self.cfgs.data_cfgs.ptx_data_files,
            )
            self.ptx_dataloader = DataLoader(
                ptx_dataset,
                collate_fn=ptx_dataset.get_collator(),
                sampler=DistributedSampler(ptx_dataset, shuffle=True),
                batch_size=self.cfgs.train_cfgs.per_device_prompt_batch_size,
            )
        else:
            self.ptx_dataloader = DataLoader(DummyDataset(len(self.prompt_only_dataloader)))

    def _init_train_engine(
        self,
        model: nn.Module,
        weight_decay: float,
        lr: float,
        lr_scheduler_type: str,
        lr_warmup_ratio: float,
        total_training_steps: int,
        ds_cfgs: dict[str, Any],
    ) -> deepspeed.DeepSpeedEngine:
        optimizer_grouped_parameters = get_optimizer_grouped_parameters(model, weight_decay)
        optimizer = FusedAdam(
            optimizer_grouped_parameters,
            lr=lr,
            betas=self.cfgs.train_cfgs.adam_betas,
        )

        lr_scheduler_update_steps = total_training_steps // ds_cfgs['gradient_accumulation_steps']
        num_warmup_steps = int(lr_scheduler_update_steps * lr_warmup_ratio)
        lr_scheduler = get_scheduler(
            name=lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=num_warmup_steps,
            num_training_steps=lr_scheduler_update_steps,
        )
        engine, *_ = deepspeed.initialize(
            model=model,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            config=ds_cfgs,
        )
        return engine

    def _init_eval_engine(
        self,
        model: nn.Module,
        ds_cfgs: dict[str, Any],
    ) -> deepspeed.DeepSpeedEngine:
        engine, *_ = deepspeed.initialize(
            model=model,
            config=ds_cfgs,
        )
        return engine

    def init_engines(self) -> None:
        """Initialize DeepSpeed engines."""
        self.total_training_steps: int = (
            len(self.prompt_only_dataloader)
            * self.cfgs.train_cfgs.epochs
            * self.cfgs.train_cfgs.update_iters
            * self.cfgs.train_cfgs.per_device_prompt_batch_size
            // self.cfgs.train_cfgs.per_device_train_batch_size
        )
        # initialize the actor model engines
        actor_ds_cfgs = copy.deepcopy(self.ds_train_cfgs)
        actor_total_training_steps = self.total_training_steps
        if self.use_ptx:
            actor_ds_cfgs['train_batch_size'] *= 2
            actor_ds_cfgs['gradient_accumulation_steps'] *= 2
            actor_total_training_steps *= 2
        self.actor_model = self._init_train_engine(
            model=self.actor_model,
            weight_decay=self.cfgs.train_cfgs.actor_weight_decay,
            lr=self.cfgs.train_cfgs.actor_lr,
            lr_scheduler_type=self.cfgs.train_cfgs.actor_lr_scheduler_type,
            lr_warmup_ratio=self.cfgs.train_cfgs.actor_lr_warmup_ratio,
            total_training_steps=actor_total_training_steps,
            ds_cfgs=actor_ds_cfgs,
        )
        # initialize the actor reference model engines
        self.actor_reference_model = self._init_eval_engine(
            model=self.actor_reference_model,
            ds_cfgs=self.ds_eval_cfgs,
        )
        self.actor_reference_model.eval()
        # initialize the critic model engines
        self.reward_critic_model = self._init_train_engine(
            model=self.reward_critic_model,
            weight_decay=self.cfgs.train_cfgs.critic_weight_decay,
            lr=self.cfgs.train_cfgs.critic_lr,
            lr_scheduler_type=self.cfgs.train_cfgs.critic_lr_scheduler_type,
            lr_warmup_ratio=self.cfgs.train_cfgs.critic_lr_warmup_ratio,
            total_training_steps=self.total_training_steps,
            ds_cfgs=self.ds_train_cfgs,
        )
        self.reward_model = self._init_eval_engine(
            model=self.reward_model,
            ds_cfgs=self.ds_eval_cfgs,
        )
        self.reward_model.eval()
        # setup the gradient checkpointing
        if self.cfgs.train_cfgs.actor_gradient_checkpointing:
            self.actor_model.gradient_checkpointing_enable()
        if self.cfgs.train_cfgs.critic_gradient_checkpointing:
            self.reward_critic_model.gradient_checkpointing_enable()

    def set_train(self, mode: bool = True) -> None:
        """Set training mode for all models."""
        if mode:
            self.actor_model.train()
            self.reward_critic_model.train()
            if self.cfgs.train_cfgs.actor_gradient_checkpointing:
                self.actor_model.gradient_checkpointing_enable()
        else:
            self.actor_model.eval()
            self.reward_critic_model.eval()
            if self.cfgs.train_cfgs.actor_gradient_checkpointing:
                self.actor_model.gradient_checkpointing_disable()
        return

    def split_ptx_micro_batches(
        self,
        ptx_batch: dict[str, torch.Tensor],
    ) -> list[dict[str, torch.Tensor]]:
        """Split a batch of PTX samples into micro-batches."""
        micro_batches = []
        total_batch_size = ptx_batch['input_ids'].size(0)
        micro_batch_size = self.cfgs.train_cfgs.per_device_train_batch_size
        for i in range(0, total_batch_size, micro_batch_size):
            micro_batch = {key: value[i : i + micro_batch_size] for key, value in ptx_batch.items()}
            micro_batches.append(micro_batch)
        return micro_batches

    def actor_step(self, mini_prompt_only_batch: PromptOnlyBatch) -> dict[str, Any]:
        actor_batch = copy.deepcopy(mini_prompt_only_batch)
        sequences = self.actor_model.module.generate(
            **mini_prompt_only_batch,
            generation_config=self.generation_config,
            synced_gpus=True,
            do_sample=True,
        )
        attention_mask = torch.logical_and(
            sequences.not_equal(self.tokenizer.pad_token_id),
            sequences.not_equal(self.tokenizer.unk_token_id),
        )
        actor_batch['input_ids'] = sequences
        actor_batch['attention_mask'] = attention_mask

        return actor_batch

    def reward_model_step(self, actor_batch: PromptOnlyBatch) -> dict[str, Any]:
        reward_batch = copy.deepcopy(actor_batch)
        if self.reward_tokenizer is not self.tokenizer:
            reward_tokenize_output = batch_retokenize(
                actor_batch['input_ids'],
                src_tokenizer=self.tokenizer,
                dest_tokenizer=self.reward_tokenizer,
                skip_special_tokens=True,
                device=self.args.device,
            )
            reward_batch['input_ids'] = reward_tokenize_output['input_ids']
            reward_batch['attention_mask'] = reward_tokenize_output['attention_mask']

        reward_batch['reward'] = self.reward_model(**reward_batch).end_scores.squeeze(dim=-1)
        reward_batch['reward_values'] = self.reward_critic_model(
            **actor_batch
        ).clipped_scores.squeeze(dim=-1)[:, :-1]

        return reward_batch

    @torch.no_grad()
    def rollout(self, prompt_only_batch: PromptOnlyBatch) -> list[dict[str, Any]]:
        """Rollout a batch of experiences."""
        # freeze the model for rolling out
        self.set_train(mode=False)

        total_batch_size = prompt_only_batch['input_ids'].size(0)
        micro_batch_size = self.cfgs.train_cfgs.per_device_train_batch_size
        micro_inference_batches = []
        micro_training_batches = []
        for i in range(0, total_batch_size, micro_batch_size):
            mini_batch = {
                key: prompt_only_batch[key][i : i + micro_batch_size] for key in prompt_only_batch
            }

            # actor generation
            actor_batch = self.actor_step(mini_batch)
            # reward model and reward critic model scoring
            reward_batch = self.reward_model_step(actor_batch)
            # calculate the log probabilities
            logits = self.actor_model(**actor_batch).logits
            ref_logits = self.actor_reference_model(**actor_batch).logits
            log_probs = gather_log_probabilities(logits[:, :-1], actor_batch['input_ids'][:, 1:])
            ref_log_probs = gather_log_probabilities(
                ref_logits[:, :-1], actor_batch['input_ids'][:, 1:]
            )

            micro_training_batch = {}
            micro_training_batch['prompt_idx'] = mini_batch['input_ids'].size(-1) - 1
            micro_training_batch['log_probs'] = log_probs
            micro_training_batch['ref_log_probs'] = ref_log_probs
            micro_training_batch['reward'] = reward_batch['reward']
            micro_training_batch['reward_values'] = reward_batch['reward_values']

            mini_batch['input_ids'] = reward_batch['input_ids']
            mini_batch['attention_mask'] = actor_batch['attention_mask']
            # add rollout results to the batches
            micro_inference_batches.append(mini_batch)
            micro_training_batches.append(micro_training_batch)

        # unfreeze the model for training
        self.set_train()

        return micro_inference_batches, micro_training_batches

    def actor_loss_fn(
        self,
        log_probs: torch.Tensor,  # size = (B, L - S)
        old_log_probs: torch.Tensor,  # size = (B, L - S)
        advantages: torch.Tensor,  # size = (B, L - S)
        mask: torch.BoolTensor,  # size = (B, L - S)
    ) -> torch.Tensor:  # size = ()
        # size = (B, L - S)
        ratios = torch.exp(log_probs - old_log_probs)
        surrogate1 = advantages * ratios
        surrogate2 = advantages * torch.clamp(
            ratios,
            1.0 - self.clip_range_ratio,
            1.0 + self.clip_range_ratio,
        )
        surrogate = torch.minimum(surrogate1, surrogate2)
        return -masked_mean(surrogate, mask)  # size = ()

    def rl_step(
        self, inference_batch: dict[str, torch.Tensor], training_batch: dict[str, torch.Tensor]
    ) -> dict[str, Any]:
        """Perform a single update step with RL loss."""
        old_log_probs = training_batch['log_probs']
        ref_log_probs = training_batch['ref_log_probs']
        reward = training_batch['reward']
        old_reward_values = training_batch['reward_values']
        start = training_batch['prompt_idx']

        input_ids = inference_batch['input_ids']
        attention_mask = inference_batch['attention_mask']

        sequence_mask = attention_mask[:, 1:]

        with torch.no_grad():
            old_rewards = self.add_kl_divergence_regularization(
                reward,
                old_log_probs,
                ref_log_probs,
                sequence_mask,
            )
            reward_advantages, reward_returns = self.get_advantages_and_returns(
                old_reward_values,
                old_rewards,
                sequence_mask,
                start,
            )

        logits = self.actor_model(**inference_batch, use_cache=False).logits
        log_probs = gather_log_probabilities(logits[:, :-1], input_ids[:, 1:])
        actor_loss = self.actor_loss_fn(
            log_probs[:, start:],
            old_log_probs[:, start:],
            reward_advantages,
            sequence_mask[:, start:],
        )
        self.actor_model.backward(actor_loss)
        self.actor_model.step()

        reward_values = self.reward_critic_model(**inference_batch).clipped_scores
        reward_values = reward_values.squeeze(dim=-1)[:, :-1]
        reward_critic_loss = self.critic_loss_fn(
            reward_values[:, start:],
            old_reward_values[:, start:],
            reward_returns,
            sequence_mask[:, start:],
        )
        self.reward_critic_model.backward(reward_critic_loss)
        self.reward_critic_model.step()

        with torch.no_grad():
            mask = sequence_mask[:, start:]
            kl_divergence = ((old_log_probs - ref_log_probs)[:, start:] * mask).sum(dim=-1).mean()
            mean_generated_length = mask.sum(dim=-1).float().mean()
            max_generated_length = mask.sum(dim=-1).float().max()

            reward = reward.mean()
            reward_with_kl_penalty = (old_rewards[:, start:] * mask).sum(dim=-1).mean()
            reward_advantage = masked_mean(reward_advantages, mask)
            reward_return = masked_mean(reward_returns, mask)
            reward_value = masked_mean(reward_values[:, start:], mask)

            actor_loss = get_all_reduce_mean(actor_loss)
            reward_critic_loss = get_all_reduce_mean(reward_critic_loss)
            reward = get_all_reduce_mean(reward)
            reward_with_kl_penalty = get_all_reduce_mean(reward_with_kl_penalty)
            reward_advantage = get_all_reduce_mean(reward_advantage)
            reward_return = get_all_reduce_mean(reward_return)
            reward_value = get_all_reduce_mean(reward_value)
            kl_divergence = get_all_reduce_mean(kl_divergence)
            mean_generated_length = get_all_reduce_mean(mean_generated_length)
            max_generated_length = get_all_reduce_max(max_generated_length)

        dist.barrier()

        return {
            'train/actor_loss': actor_loss.item(),
            'train/reward_critic_loss': reward_critic_loss.item(),
            'train/reward': reward.item(),
            'train/reward_with_kl_penalty': reward_with_kl_penalty.item(),
            'train/reward_advantage': reward_advantage.item(),
            'train/reward_return': reward_return.item(),
            'train/reward_value': reward_value.item(),
            'train/kl_divergence': kl_divergence.item(),
            'train/actor_lr': self.actor_model.optimizer.param_groups[0]['lr'],
            'train/reward_critic_lr': self.reward_critic_model.optimizer.param_groups[0]['lr'],
            'train/mean_generated_length': mean_generated_length.item(),
            'train/max_generated_length': max_generated_length.item(),
        }

    def get_advantages_and_returns(
        self,
        values: torch.Tensor,
        rewards: torch.Tensor,
        sequence_mask: torch.BoolTensor,
        start: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute advantages and returns using Generalized Advantage Estimation (GAE)."""
        # Modified from https://github.com/CarperAI/trlx/blob/main/trlx/models/modeling_ppo.py
        last_gae_lambda = 0.0
        advantages_reversed = []
        values = values * sequence_mask
        rewards = rewards * sequence_mask
        length = rewards.size(-1)
        for t in reversed(range(start, length)):  # pylint: disable=invalid-name
            next_values = values[:, t + 1] if t < length - 1 else 0.0
            delta = rewards[:, t] + self.gamma * next_values - values[:, t]
            last_gae_lambda = delta + self.gamma * self.gae_lambda * last_gae_lambda
            advantages_reversed.append(last_gae_lambda)
        advantages = torch.stack(advantages_reversed[::-1], dim=1)
        returns = advantages + values[:, start:]
        return advantages.detach(), returns

    def ptx_step(self, ptx_batch: dict[str, torch.Tensor]) -> dict[str, Any]:
        """Perform a single update step with PTX loss."""
        ptx_loss = self.actor_model(**ptx_batch).loss
        self.actor_model.backward(self.ptx_coeff * ptx_loss)
        self.actor_model.step()
        ptx_loss = get_all_reduce_mean(ptx_loss)
        return {
            'train/ptx_loss': ptx_loss.item(),
        }

    def train(self) -> None:
        """Train the model."""
        self.logger.print('***** Running training *****')

        progress_bar = tqdm(
            total=self.total_training_steps,
            desc=f'Training 1/{self.cfgs.train_cfgs.epochs} epoch',
            position=0,
            leave=True,
            disable=not is_main_process(),
        )

        if self.cfgs.data_cfgs.eval_datasets:
            self.logger.print('\n***** Evaluating at the beginning *****')
            self.eval()

        num_prompt_only_batches = len(self.prompt_only_dataloader)
        num_ptx_batches = len(self.ptx_dataloader)
        num_ptx_replicas = (num_prompt_only_batches + num_ptx_batches - 1) // num_ptx_batches
        for epoch in range(self.cfgs.train_cfgs.epochs):
            for prompt_only_batch, ptx_batch in zip(
                self.prompt_only_dataloader,
                itertools.chain.from_iterable([self.ptx_dataloader] * num_ptx_replicas),
            ):
                inference_batches, training_batches = self.rollout(prompt_only_batch)

                if self.use_ptx:
                    ptx_batches = self.split_ptx_micro_batches(ptx_batch)
                else:
                    ptx_batches = [None for _ in range(len(inference_batches))]
                torch.cuda.empty_cache()

                for _ in range(self.cfgs.train_cfgs.update_iters):
                    for inference_batch, training_batch, ptx_batch in zip(
                        inference_batches, training_batches, ptx_batches
                    ):
                        rl_info = self.rl_step(inference_batch, training_batch)

                        torch.cuda.empty_cache()
                        self.logger.log(rl_info, step=self.global_step)
                        if self.use_ptx:
                            ptx_info = self.ptx_step(ptx_batch)
                            torch.cuda.empty_cache()
                            self.logger.log(ptx_info, step=self.global_step)

                        self.global_step += 1
                        progress_bar.set_description(
                            f'Training {epoch + 1}/{self.cfgs.train_cfgs.epochs} epoch '
                            f'(reward {rl_info["train/reward"]:.4f})',
                        )
                        progress_bar.update(1)

                        if self.global_step % self.cfgs.logger_cfgs.save_interval == 0:
                            self.logger.print(f'Saving checkpoint at step {self.global_step} ...')
                            self.save(tag=self.global_step)
                            self.logger.print('Checkpoint saved.')

                        if (
                            self.cfgs.data_cfgs.eval_datasets
                            and self.cfgs.train_cfgs.eval_strategy == 'steps'
                            and self.global_step % self.cfgs.train_cfgs.eval_interval == 0
                        ):
                            self.logger.print(
                                f'\n***** Evaluating at step {self.global_step} *****',
                            )
                            self.eval()

            if self.cfgs.data_cfgs.eval_datasets and self.cfgs.train_cfgs.eval_strategy == 'epoch':
                self.logger.print(
                    f'\n***** Evaluating at epoch {epoch + 1}/{self.cfgs.train_cfgs.epochs} *****',
                )
                self.eval()

    def eval(self) -> dict[str, Any]:
        """Evaluate the model on the evaluation dataset."""
        if self.eval_dataloader is None:
            return {}

        self.set_train(mode=False)
        prompts: list[str] = []
        generateds: list[str] = []
        eval_dataloader = tqdm(
            self.eval_dataloader,
            desc='Evaluating',
            disable=not is_main_process(),
        )
        for batch in eval_dataloader:
            with torch.no_grad():
                seq = self.actor_model.module.generate(
                    **batch,
                    max_length=self.cfgs.model_cfgs.model_max_length,
                    synced_gpus=True,
                    do_sample=True,
                )

            dist.barrier()
            prompt = self.tokenizer.batch_decode(batch['input_ids'], skip_special_tokens=True)
            generated = self.tokenizer.batch_decode(seq, skip_special_tokens=True)
            generated = [text[len(prompt[i]) :] for i, text in enumerate(generated)]
            prompts.extend(prompt)
            generateds.extend(generated)
        # Display result in main process
        if is_main_process():
            columns = ['Prompt', 'Generated']
            rows = list(zip(prompts, generateds))
            self.logger.print_table(
                title='Evaluating...',
                columns=columns,
                rows=rows,
                max_num_rows=5,
            )
        dist.barrier()

        self.set_train()

    def get_advantages_and_returns(
        self,
        values: torch.Tensor,
        rewards: torch.Tensor,
        sequence_mask: torch.BoolTensor,
        start: int,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Compute advantages and returns using Generalized Advantage Estimation (GAE)."""
        # Modified from https://github.com/CarperAI/trlx/blob/main/trlx/models/modeling_ppo.py
        last_gae_lambda = 0.0
        advantages_reversed = []
        values = values * sequence_mask
        rewards = rewards * sequence_mask
        length = rewards.size(-1)
        for t in reversed(range(start, length)):  # pylint: disable=invalid-name
            next_values = values[:, t + 1] if t < length - 1 else 0.0
            delta = rewards[:, t] + self.gamma * next_values - values[:, t]
            last_gae_lambda = delta + self.gamma * self.gae_lambda * last_gae_lambda
            advantages_reversed.append(last_gae_lambda)
        advantages = torch.stack(advantages_reversed[::-1], dim=1)
        returns = advantages + values[:, start:]
        return advantages.detach(), returns

    def critic_loss_fn(
        self,
        values: torch.Tensor,  # size = (B, L - S)
        old_values: torch.Tensor,  # size = (B, L - S)
        returns: torch.Tensor,  # size = (B, L - S)
        mask: torch.BoolTensor,  # size = (B, L - S)
    ) -> torch.Tensor:  # size = ()
        """Compute critic loss."""
        # size = (B, L - S)
        values_clipped = torch.clamp(
            values,
            old_values - self.clip_range_value,
            old_values + self.clip_range_value,
        )
        vf_loss1 = torch.square(values - returns)
        vf_loss2 = torch.square(values_clipped - returns)
        return 0.5 * masked_mean(torch.maximum(vf_loss1, vf_loss2), mask)  # size = ()

    def add_kl_divergence_regularization(
        self,
        reward: torch.Tensor,  # size = (B,)
        log_probs: torch.Tensor,  # size = (B, L)
        ref_log_probs: torch.Tensor,  # size = (B, L)
        sequence_mask: torch.BoolTensor,  # size = (B, L)
    ) -> torch.Tensor:  # size = (B, L)
        """Add KL divergence regularization on scalar rewards."""
        end_index = torch.cat([m.nonzero()[-1] for m in sequence_mask])  # size = (B,)

        # size = (B, L)
        kl_divergence_estimate = log_probs - ref_log_probs
        kl_penalty_rewards = -self.kl_coeff * kl_divergence_estimate
        rewards = torch.scatter_add(
            kl_penalty_rewards,
            dim=-1,
            index=end_index.unsqueeze(dim=-1),
            src=reward.to(kl_penalty_rewards.dtype).unsqueeze(dim=-1),
        )
        return torch.clamp(rewards, min=-self.clip_range_score, max=self.clip_range_score)

    def save(
        self,
        model: deepspeed.DeepSpeedEngine | None = None,
        tag: int | None = None,
    ) -> None:
        """Save model and tokenizer in Hugging Face format."""
        dist.barrier()

        if model is None:
            model = self.actor_model  # pylint: disable=no-member

        self.logger.print(f'Saving model to "{self.cfgs.logger_cfgs.output_dir}" ...')

        output_config_file = os.path.join(self.cfgs.logger_cfgs.output_dir, CONFIG_NAME)
        model_to_save: PreTrainedModel = getattr(model, 'module', model)

        if is_main_process():
            model_to_save.config.to_json_file(output_config_file)
            self.tokenizer.save_pretrained(self.cfgs.logger_cfgs.output_dir)
            self.processor.save_pretrained(self.cfgs.logger_cfgs.output_dir)

        self.logger.print('Saving 16-bit model...')
        save_file_name = f'pytorch_model_{tag}.bin' if tag else 'pytorch_model.bin'
        model.save_16bit_model(self.cfgs.logger_cfgs.output_dir, save_filename=save_file_name)

        self.logger.print('Model saved!')


def main():
    # setup distribution training
    deepspeed.init_distributed()
    current_device = get_current_device()
    torch.cuda.set_device(current_device)

    # read default configs from the yaml file
    dict_cfgs, ds_cfgs = read_cfgs(mode='train', task='ppo')

    # get custom configs from command line
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[1::2]]
    values = list(unparsed_args[2::2])
    unparsed_args = dict(zip(keys, values))
    for k, v in unparsed_args.items():
        dict_cfgs = update_dict(dict_cfgs, custom_cfgs_to_dict(k, v))

    # setup training
    cfgs = dict_to_namedtuple(dict_cfgs)
    seed_everything(cfgs.train_cfgs.seed)

    # finetune the model
    trainer = PPOTrainer(cfgs=cfgs, ds_cfgs=ds_cfgs)
    trainer.train()
    trainer.save()


if __name__ == '__main__':
    sys.exit(main())

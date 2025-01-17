<!-- markdownlint-disable first-line-h1 -->
<!-- markdownlint-disable html -->

<div align="center">
  <img src="assets/logo.jpg" width="390"/>
  <div>&nbsp;</div>
  <div align="center">
    <b><font size="5">project website</font></b>
    <sup>
      <a href="https://space.bilibili.com/3493095748405551?spm_id_from=333.337.search-card.all.click">
        <i><font size="4">HOT</font></i>
      </a>
    </sup>
    &nbsp;&nbsp;&nbsp;&nbsp;
    <b><font size="5">PKU-Alignment Team</font></b>
    <sup>
      <a href="https://space.bilibili.com/3493095748405551?spm_id_from=333.337.search-card.all.click">
        <i><font size="4">welcome</font></i>
      </a>
    </sup>
  </div>
  <div>&nbsp;</div>


[![PyPI](https://img.shields.io/pypi/v/align-anything?logo=pypi)](https://pypi.org/project/align-anything)
[![License](https://img.shields.io/github/license/PKU-Alignment/align-anything?label=license)](#license)

📘Documentation |
[🚀Features](#features) |
[🆕Update News](#news) |
[🛠️Installation](#installation) |
[👀Training](#train) |
[🤔Reporting Issues](#report-issues)
</div>

<div align="center">

English | [简体中文](README_zh-CN.md) | [Our 100K Datasets](https://huggingface.co/datasets/PKU-Alignment/Align-Anything-Instruction-100K) | 👋 加入我们的[微信群](assets/wechat.jpg)

</div>

Align-Anything is an open-source alignment framework for academic research based on DeepSpeed or NeMo (currently in development). It aims to align various modality large models (any-to-any models), including LLMs, VLMs, and others, with human intentions and values. More details about the definition and milestones of alignment for LLMs and other related information can be found in [AI Alignment](https://alignmentsurvey.com).

### Features

- Highly Modular Framework: Our framework offers a comprehensive collection of diverse alignment algorithms tailored for model alignment across various modalities. Its versatility stems from the abstraction of different algorithm types and a well-designed API, allowing users to easily modify and customize the code for different tasks.
- Support for Various Model Fine-Tuning: The framework includes fine-tuning capabilities for models such as LLaMA, LLaVA, Gemma, Qwen, Baichuan, and others (see [model-zoo](https://github.com/PKU-Alignment/align-anything/blob/main/Model-Zoo.md)).
- Support Alignment Fine-Tuning over Any Modality: It supports fine-tuning alignments for different modality model, including LLMs, VLMs, and other modalities (see [Development Roadmap](#development-roadmap)).
- Support Various Alignment Algorithms: The framework supports various alignment algorithms, including SFT, DPO, PPO, and others (see [example](https://github.com/PKU-Alignment/align-anything/tree/main/examples)).

#### Development Roadmap

We have a roadmap for future development work `align-anything`:

- [ ] Support alignment algorithms over the `diffusion model`, `text to any generation model` and other `vision-language model`.
- [ ] Support diverse parameter sizes including `LoRA`, `QLoRA`.
- [ ] Support `NeMo` backbone for training, and `vllm` backbone for evaluation.

| Trainers | Text :arrow_right: Text | Image+Text :arrow_right: Text | Text :arrow_right: Image | Text :arrow_right: Video | More Modality... |
|---|---|---|---|---|---|
| SFT Trainer | :white_check_mark: | :white_check_mark: | :airplane: | :car: | :car: |
| RM Trainer | :white_check_mark: | :white_check_mark: | :airplane: | :car: | :car: |
| DPO Trainer | :white_check_mark: | :white_check_mark: | :airplane: | :car: | :car: |
| PPO Trainer | :white_check_mark: | :white_check_mark: | :airplane: | :car: | :car: |
| KTO Trainer | :white_check_mark: | :car: | :car: | :car: | :car: |
| ORPO Trainer | :white_check_mark: | :car: | :car: | :car: | :car: |
| SimPO Trainer | :white_check_mark: | :car: | :car: | :car: | :car: |

- :white_check_mark: : Features supported now.
- :airplane: : Features under test, would be supported as soon as possible.
- :car: : Features on going in our TODO list.

# News

- 2024-07-14 🎉We open-souce the `align-anything` framework.

# Installation

All model weights, training parameters, and tokenizers are stored in the `OUTPUT_DIR` you specified in advance.

```bash
conda create -n align-anything python==3.11
conda activate align-anything
git clone git@github.com:PKU-Alignment/align-anything.git
cd align-anything
pip install -e .
```

### Wandb Logger
We supports `wandb` logging. By default, it is set to offline. If you need to view wandb logs online, you can specify the environment variables of `WANDB_API_KEY` before starting the training:

```bash
export WANDB_API_KEY="..."  # your W&B API key here
```

### Install from Dockerfile
<details>
<summary>How to build from Docker?</summary>
1. build docker image

```bash
FROM nvcr.io/nvidia/pytorch:24.02-py3

RUN echo "export PS1='[\[\e[1;33m\]\u\[\e[0m\]:\[\e[1;35m\]\w\[\e[0m\]]\$ '" >> ~/.bashrc

WORKDIR /root/align-anything
COPY . .

RUN python -m pip install --upgrade pip \
    && pip install -e .
```

then,

```bash
docker build --tag align-anything .
```

2. run the container

```bash
docker run -it --rm \
    --gpus all \
    --ipc=host \
    --ulimit memlock=-1 \
    --ulimit stack=67108864 \
    --mount type=bind,source=<host's mode path>,target=<docker's mode path> \
    test_docker
```

</details>


# Train

## Quick Start

Quick start examples can be found at [here](./examples/)

To prepare for training, all scripts are located in the `./scripts`. Parameters that require user input have been left empty and must be filled in prior to initiating the training process. For example, for `ppo.sh`:

```bash
ACTOR_MODEL_NAME=""
REWARD_MODEL_NAME=""
CRITIC_MODEL_NAME=""
TRAIN_DATASETS=""
TRAIN_TEMPLATE=""
PTX_DATASET=""
PTX_TEMPLATE=""
OUTPUT_DIR=""

source ./setup.sh

deepspeed \
  --master_port ${MASTER_PORT} \
  --module align_anything.trainers.ppo \
  --actor_model_name_or_path ${ACTOR_MODEL_NAME} \
  --reward_model_name_or_path ${REWARD_MODEL_NAME} \
  --reward_critic_model_name_or_path ${CRITIC_MODEL_NAME} \
  --train_datasets ${TRAIN_DATASETS} \
  --train_split train \
  --train_template ${TRAIN_TEMPLATE} \
  --ptx_datasets ${PTX_DATASET} \
  --ptx_split train \
  --ptx_template ${PTX_TEMPLATE} \
  --output_dir ${OUTPUT_DIR}
```

<!-- TODO -->
- `ACTOR_MODEL_NAME`: The model to be fine-tuned, typically one that has already undergone initial supervised fine-tuning, like `PKU-Alignment/alpaca-7b-reproduced`.
- `REWARD_MODEL_NAME`: A model with a score output layer. Run `rm.sh` to train the reward model and obtain its path.
- `CRITIC_MODEL_NAME`: The model used for RLHF value function estimation, typically set to be the same as `REWARD_MODEL_NAME`.
- `TRAIN_DATASET`: The training dataset for RLHF, such as `PKU-Alignment/PKU-SafeRLHF`.
- `TRAIN_TEMPLATE`: The training template for RLHF, such as `PKU-Alignment/PKU-SafeRLHF`.
- `PTX_DATASET`: The supervised learning dataset to aid RLHF fine-tuning, like `tatsu-lab/alpaca`.
- `PTX_TEMPLATE`: The template for auxiliary supervised learning dataset in RLHF needs to be specified before training, and in this case, it is `Dialogue`.
- `OUTPUT_DIR`: The directory where you want to save the trained model, logging, and others.

### Some Training Bugs
1. If you encounter errors during the training process:

```bash
No such file or directory: ':/usr/local/cuda/bin/nvcc'
```

To include the CUDA installation path and set the environment variables, modify the script as follows:

```bash
export CUDA_HOME="/usr/local/cuda"
```
or
```bash
export CUDA_HOME=$CONDA_PREFIX
```

The specific path depends on your `cuda` path.

## Customized Dataset

Align-anything offers a highly scalable dataset registration interface, enabling users to embed customized datasets simply by designing and specifying their `template.py`. 

Taking [PKU-Alignment/PKU-SafeRLHF](https://huggingface.co/datasets/PKU-Alignment/PKU-SafeRLHF) as an example, we illustrate here how to design the template and incorporate it into a complete RLHF workflow.

The data key-value pairs for PKU-Alignment/PKU-SafeRLHF are as follows:

```python
{
  'prompt': '...',
  'response_0': '...',
  'response_1': '...',
  'better_response_id': 0
}
```

We first need to create a new template named PKUSafeRLHF for this dataset, and specify the required parameters such as system_prompt.

```python
@register_template('PKUSafeRLHF')
class PKUSafeRLHF(Template):
    system_prompt: str = 'BEGINNING OF CONVERSATION: '
    user_prompt: str = 'USER: {input} '
    assistant_prompt: str = 'ASSISTANT:{output}'
    split_token: str = 'ASSISTANT:'
```

### Reward modeling

The reward modeling requires the user to provide a dictionary with data keys as follows:

```python
{
  'better_text': '...',
  'worse_text': '...',
}
```

Therefore, the user needs to implement a key-value transformation logic in `align-anything/configs/template.py`, for instance, in this case:

```python
@register_template('PKUSafeRLHF')
class PKUSafeRLHF(Dialogue):

    def format_sample(self, raw_sample: dict[str, Any]) -> dict[str, Any]:
        metrics = raw_sample['better_response_id']
        better_response = raw_sample[f'response_{int(metrics)}']
        worse_response = raw_sample[f'response_{1-int(metrics)}']
        prompt = raw_sample['prompt']

        formatted_better_output = (
            f'{self.system_prompt}'
            f'{self.user_prompt.format(input=prompt)}'
            f'{self.assistant_prompt.format(output=better_response)}'
        )
        formatted_worse_output = (
            f'{self.system_prompt}'
            f'{self.user_prompt.format(input=prompt)}'
            f'{self.assistant_prompt.format(output=worse_response)}'
        )

        return {
            'better_text': formatted_better_output,
            'worse_text': formatted_worse_output,
        }
```

Here, `format_sample` parses the keys in the PKU-Alignment/PKU-SafeRLHF dataset, determines which response is better based on the `better_response_id`, and subsequently invokes previously defined parameters such as `system_prompt` to implement the transformation of key-value pairs.

### RL fine-tuning

During the RL fine-tuning phase, the model requires generation based on prompts within the dataset. Consequently, users need to implement key-value conversion in `template.py` using the following function:

```python
@register_template('PKUSafeRLHF')
class PKUSafeRLHF(Template):
    system_prompt: str = 'BEGINNING OF CONVERSATION: '
    user_prompt: str = 'USER: {input} '
    assistant_prompt: str = 'ASSISTANT:{output}'
    split_token: str = 'ASSISTANT:'

    def format_prompt_only_sample(self, raw_sample: dict[str, Any]) -> dict[str, Any]:
        prompt = raw_sample['prompt']

        formatted_prompt = (
            f'{self.system_prompt}'
            f'{self.user_prompt.format(input=prompt)}'
            f'{self.assistant_prompt.format(output="")}'
        )

        return {'text': formatted_prompt}
```

After designing the aforementioned template, you just need to specify this template by passing the `--train_template PKUSafeRLHF` argument when invoking the dataset to complete the corresponding training. Perhaps the above example still lacks specificity; therefore, we provide command references that encompass various models executing multiple algorithms on diverse datasets. You can expedite your training process by directly running or modifying these scripts [here](./examples/).

## Why do we open source align-anything?

Ensuring that the behavior of AI system aligns with human intentions and values is crucial, and alignment techniques provide an effective solution. For large language models (LLMs), methods such as reinforcement learning with human feedback (RLHF) and direct preference optimization (DPO) have significantly improved performance and safety. As models evolve to handle any-modality inputs and outputs, effectively aligning them remains a current research challenge. `Align-Anything` framework integrates alignment tuning across modalities using well-designed interfaces and advanced abstractions, offering a comprehensive testbed for research.

### Report Issues
If you have any questions in the process of using Align-Anything, don't hesitate to ask your questions on [the GitHub issue page](https://github.com/PKU-Alignment/align-anything/issues/new/choose), we will reply to you in 2-3 working days.


## Citation
Please cite the repo if you use the data or code in this repo.
```
@misc{align_anything,
  author = {PKU-Alignment Team},
  title = {Align Anything: Training Any Modality Model with Feedback},
  year = {2024},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/PKU-Alignment/align-anything}},
}
```

## License

Align-Anything is released under Apache License 2.0.

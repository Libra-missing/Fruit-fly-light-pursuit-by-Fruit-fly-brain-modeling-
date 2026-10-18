# ROS 2 果蝇趋光仿真

<p align="center">
  <b>用连接组规模脉冲神经网络与强化学习基准驱动虚拟果蝇</b><br>
  <i>基于 snnTorch 果蝇全脑模型的 ROS 2 工程项目</i>
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="#演示对比">演示对比</a> ·
  <a href="#概述">概述</a> ·
  <a href="#性能对比">性能对比</a> ·
  <a href="#架构">架构</a> ·
  <a href="#安装">安装</a> ·
  <a href="#使用方法">使用方法</a>
</p>

---

## 演示对比

以下是生物脉冲神经网络（SNN）与训练好的强化学习（RL）模型在同一 ROS 2 环境中的运行对比：

| 生物大脑 (12.7 万神经元 SNN) | 强化学习智能体 (PPO) |
| :---: | :---: |
| *异步计算，耗时巨大* | *同步计算，亚毫秒级推理* |
| <img src="docs/demo_snn.gif" width="400" alt="SNN"> | <img src="docs/demo_rl.gif" width="400" alt="RL"> |
| **[此处替换为你的 SNN 动图]** | **[此处替换为你的 RL 动图]** |

> *(注：SNN 在 CPU 上运行极其缓慢，需要异步线程解耦，导致动作卡顿。而 RL 模型以 30Hz 丝滑运行。)*

---

## 概述

本项目把一个 **12.7 万神经元的成年果蝇脉冲大脑模型**，和一个用 ROS 2 渲染的 **2D 虚拟果蝇** 闭合成一个趋光控制回路。

为了评估生物启发式计算的实际性能，**我还使用完全相同的运动学约束和动作空间，训练了一个强化学习（PPO）智能体作为基准对比。**

其中生物大脑模型复用了 **Michał Antropik** 的 snnTorch 稀疏移植：
> **上游大脑模型**：https://github.com/Neuromorphicism/fly-brain-snntorch
>
> 论文：P. K. Shiu, G. R. Sterne, N. Spiller et al. *"A Drosophila computational brain model reveals sensorimotor processing"* Nature, 2024.

> **关于视觉的说明**：FlyWire 中央脑不含完整的视觉系统，因此我将光强映射为了**糖味觉通路**。模型输出（MN9 运动神经元）被解码为转向指令。

---

## 性能对比 (Benchmark)

为什么引入强化学习？这是我在测试后得到的性能对比：

| 指标 | SNN 大脑 (`fly_brain_ros.py`) | 强化学习智能体 (`rl_brain_ros.py`) |
|---|---|---|
| **模型规模** | 127,400 神经元 | ~10,000 参数 (MLP) |
| **推理延迟** | ~500 ms / 步 (CPU) | < 1 ms / 步 (CPU) |
| **控制频率** | 异步 (2Hz 仿真, 10Hz 控制) | 同步 (30Hz) |
| **轨迹质量** | 卡顿，纯反应式 | 丝滑，具备预测性截击 |
| **训练方式** | 进化 (生物连接组) | PPO (强化学习) |

**核心结论**：生物连接组具有极致的生物学真实性，但在标准 CPU 上带来了巨大的计算开销。通过简化的奖励函数（距离缩减）训练 RL 智能体，我们在同一环境下实现了 **500 倍的推理提速**和极其平滑的控制。

---

## 架构

```mermaid
flowchart LR
    ENV["fly_env.py<br/>（pygame + ROS）"]
    
    SNN["fly_brain_ros.py<br/>（ROS + snnTorch）"]
    RL["rl_brain_ros.py<br/>（ROS + PyTorch）"]

    ENV -- "/light/pose, /fly/pose" --> SNN
    SNN -- "/fly/cmd_vel" --> ENV
    
    ENV -- "/light/pose, /fly/pose" --> RL
    RL -- "/fly/cmd_vel" --> ENV
```

**SNN 信号流**：感知 → 泊松脉冲编码 → 全脑 LIF 网络 → MN9 解码 → 速度控制。
**RL 信号流**：归一化状态观测 → MLP 前向推理 → 速度控制。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `fly_env.py` | pygame 2D 世界，ROS 环境节点 |
| `fly_brain_ros.py` | 生物 SNN 大脑线程 + ROS 控制器 |
| `rl_brain_ros.py` | 强化学习（PyTorch）ROS 控制器 |
| `fly_rl_env.py` | 用于 RL 训练的自定义 Gymnasium 环境（物理严格对齐） |
| `train_ppo.py` | PPO 智能体训练脚本及权重导出工具 |
| `make_assets.py` | 生成 `assets/` 下的素材图片 |
| `data/` | FlyWire v630 完整性 CSV + 连接组 parquet |

---

## 环境与安装

### 1. ROS 2（Jazzy 或更新）
```bash
source /opt/ros/jazzy/setup.bash
```

### 2. Python 环境
```bash
conda activate ros2_env
pip install pygame pillow
# 两个模型都需要 PyTorch 支持
```

> ⚠️ **不要**在 ROS 环境里用 `conda install pygame` —— 会和 Qt 依赖冲突。

### 3. 大脑数据（仅 SNN 需要）
下载 FlyWire v630 文件放到 `data/`：
- `2023_03_23_completeness_630_final.csv`
- `2023_03_23_connectivity_630_final.parquet`
来源：https://github.com/philshiu/Drosophila_brain_model

### 4. RL 训练环境（可选）
如果你想重新训练 RL 智能体，建议新建一个环境避免冲突：
```bash
conda create -n fly_rl python=3.10
conda activate fly_rl
pip install torch gymnasium stable-baselines3 numpy
python train_ppo.py
# 这会生成 `actor_weights.pt` 给 ROS 节点使用
```

---

## 使用方法

开**两个终端**（使用 `ros2_env` 环境）。

**终端 A —— 环境**
```bash
python fly_env.py
```

**终端 B —— 选择你的大脑**
*选项 1：生物 SNN（需要下载数据）*
```bash
python fly_brain_ros.py
```
*选项 2：强化学习模型（极速丝滑）*
```bash
python rl_brain_ros.py
```

---

## 致谢

本项目是 ROS 2 下游集成及 RL 基准测试。核心 SNN 大脑模型**不是我的工作**，它是 **Michał Antropik** 的 snnTorch 移植：
- **上游仓库**：https://github.com/Neuromorphicism/fly-brain-snntorch
- **原始 Brian2 模型**：https://github.com/philshiu/Drosophila_brain_model

## 许可证
上游大脑模型及其数据采用 MIT 许可证。本 ROS 2 集成与 RL 基准测试采用相同条款发布。

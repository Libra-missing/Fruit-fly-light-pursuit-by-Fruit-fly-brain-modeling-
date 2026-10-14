#!/usr/bin/env python3
"""
fly_brain_ros.py — 全脑模型 + 趋光控制
fly_brain_ros.py — Whole-brain model + phototaxis controller

订阅 / Subscribe:
  /fly/pose    (Pose2D, pygame 像素, y 向下 / pygame pixel, y downward)
  /light/pose  (Pose2D)
发布 / Publish:
  /fly/cmd_vel (Twist)

大脑在独立线程中异步仿真，避免阻塞 ROS 控制循环。
The brain runs asynchronously in a separate thread so it does not block
the ROS control loop.

基于 / Based on:
  Shiu et al., Nature 634, 210–219 (2024)
  snnTorch port by M. Antropik (Neuromorphicism)
"""

import math
import threading
import time
import datetime
from pathlib import Path

import pandas as pd
import torch
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, Pose2D

# ============================================================
#                      配置 / Configuration
# ============================================================

BASE_DIR = Path(__file__).resolve().parent    # 仓库根目录 / repo root

CONFIG = {
    # 神经元列表 CSV / neuron list CSV
    "path_completeness": BASE_DIR / "data" / "2023_03_23_completeness_630_final.csv",
    # 连接组 parquet / connectivity parquet
    "path_connectivity": BASE_DIR / "data" / "2023_03_23_connectivity_630_final.parquet",
    # 缓存的稀疏权重 / cached sparse weights
    "path_saved_weights": BASE_DIR / "model" / "fly_brain_connections_weights.pth",
}

# 生物物理参数 / biophysical parameters
PARAMS = {
    "dt": 1.0,                    # 时间步（ms）/ time step (ms)
    "tau_membrane": 20.0,         # 膜时间常数（ms）/ membrane time constant
    "threshold_level": 1.0,       # 放电阈值 / firing threshold
    "reset_level": 0.0,           # 复位电位 / reset potential
    "weight_per_synapse": 0.05,   # 单突触基础权重 / base synaptic weight
}

# 是否从硬盘加载已缓存的权重 / load cached weights from disk
LOAD_WEIGHTS_FROM_DISK = True
# 是否把构建好的权重存到硬盘 / save built weights to disk
SAVE_WEIGHTS_TO_DISK   = False

# MN9 (CB0701) 运动神经元 FlyWire ID / MN9 motor neuron FlyWire ID
# 来源 / source: Virtual Fly Brain / FlyBase
MN9_FLYWIRE_ID = 720575940630329820

# 糖感觉神经元（右侧，FlyWire v630）/ sugar GRNs (right, FlyWire v630)
# 来源 / source: philshiu/Drosophila_brain_model
SUGAR_GRN_IDS = [
    720575940624963786, 720575940630233916, 720575940637568838,
    720575940638202345, 720575940617000768, 720575940630797113,
    720575940632889389, 720575940621754367, 720575940621502051,
    720575940640649691, 720575940639332736, 720575940616885538,
    720575940639198653, 720575940620900446, 720575940617937543,
    720575940632425919, 720575940633143833, 720575940612670570,
    720575940628853239, 720575940629176663, 720575940611875570,
]

# 控制参数 / control parameters
CTRL_HZ        = 10       # ROS 控制循环频率（Hz）/ control loop frequency
BRAIN_HZ       = 2        # 大脑仿真频率（Hz）/ brain simulation frequency
TURN_GAIN      = 3.5      # 转向增益 / steering gain
BASE_SPEED     = 1.2      # 直行速度 / forward speed
SLOW_SPEED     = 0.3      # 转向时速度 / speed while turning
SLOW_ANGLE_THR = 0.5      # 触发减速的角度阈值（rad）/ slow-down angle threshold

DEVICE = torch.device("cpu")   # 强制 CPU / force CPU


# ============================================================
#              权重矩阵构建 / Weight matrix construction
# ============================================================

def build_weights_matrix(conn, n, w_syn):
    """
    从连接数据构建稀疏权重矩阵（COO → 后续转 CSR）。
    Build a sparse weight matrix from connectivity data (COO, later → CSR).

    参数 / Args:
        conn:  connectome parquet DataFrame
        n:     神经元总数 / total neuron count
        w_syn: 单突触基础权重 / base synaptic weight
    """
    begin = datetime.datetime.now()
    print(f"[Weights] 开始构建 / building... {begin}")

    # 突触前 / 突触后索引 / presynaptic and postsynaptic indices
    pre  = torch.tensor(conn["Presynaptic_Index"].values,  dtype=torch.long, device=DEVICE)
    post = torch.tensor(conn["Postsynaptic_Index"].values, dtype=torch.long, device=DEVICE)

    # 兴奋 × 连接数，正值兴奋、负值抑制 / excitatory × count, +exc / -inh
    w = torch.tensor(conn["Excitatory x Connectivity"].values,
                     device=DEVICE) * w_syn

    # COO 稀疏张量 / COO sparse tensor
    indices = torch.stack([post, pre])
    mat = torch.sparse_coo_tensor(
        indices, w,
        size=(n, n), device=DEVICE,
    ).coalesce()

    print(f"[Weights] 完成 / done, 耗时 / elapsed {datetime.datetime.now() - begin}")
    return mat


# ============================================================
#              大脑仿真器（后台线程）/ Brain simulator (bg thread)
# ============================================================

class BrainSimulator:
    """
    全脑模型在独立线程中异步仿真。
    Whole-brain model simulated asynchronously in a separate thread.

    输入 / Input:  left_light, right_light (0~1)
    输出 / Output: mn9_rate (0~1 滑动平均 / running average)
    """

    def __init__(self, neurons_list, weights):
        self.neurons_list = neurons_list     # 神经元列表 / neuron list
        self.weights = weights               # 稀疏权重 / sparse weights
        self.n = len(neurons_list)           # 神经元数量 / neuron count

        # 糖 GRN 索引 / sugar GRN indices
        self.sugar_idx = [
            neurons_list.index.get_loc(nid)
            for nid in SUGAR_GRN_IDS
            if nid in neurons_list.index
        ]

        # 把糖 GRN 拆成左右两半（近似）/ split GRNs into L/R halves
        half = max(1, len(self.sugar_idx) // 2)
        self.sugar_L = self.sugar_idx[:half]
        self.sugar_R = self.sugar_idx[half:]
        print(f"[Brain] 糖 GRN / sugar GRN: {len(self.sugar_idx)}/{len(SUGAR_GRN_IDS)} 匹配 / matched "
              f"(L={len(self.sugar_L)} R={len(self.sugar_R)})")

        # MN9 索引 / MN9 index
        if MN9_FLYWIRE_ID in neurons_list.index:
            self.mn9_idx = neurons_list.index.get_loc(MN9_FLYWIRE_ID)
            print(f"[Brain] MN9 索引 / index: {self.mn9_idx}")
        else:
            self.mn9_idx = None
            print(f"[Brain] ⚠ MN9 {MN9_FLYWIRE_ID} 不在列表中 / not in list，用近似替代 / fallback")

        # 网络状态 / network state
        self.mem    = torch.zeros(self.n, device=DEVICE)     # 膜电位 / membrane potential
        self.spikes = torch.zeros(self.n, device=DEVICE)     # 上一步脉冲 / last spikes

        # 输入缓存（线程安全）/ input cache (thread-safe)
        self._left  = 0.0
        self._right = 0.0
        self._in_lock = threading.Lock()

        # 输出缓存（线程安全）/ output cache (thread-safe)
        self._mn9_rate = 0.0
        self._out_lock = threading.Lock()

        self.running = True
        # 启动仿真线程 / start simulation thread
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print("[Brain] 仿真线程已启动 / simulation thread started")

    # ---------- 对外接口 / public interface ----------
    def set_light(self, l, r):
        """设置左右光强 / set left and right light intensity."""
        with self._in_lock:
            self._left, self._right = float(l), float(r)

    def get_mn9_rate(self):
        """读取 MN9 放电率 / read MN9 firing rate."""
        with self._out_lock:
            return self._mn9_rate

    def stop(self):
        """停止仿真线程 / stop the simulation thread."""
        self.running = False
        self.thread.join(timeout=2.0)

    # ---------- 仿真主循环 / simulation loop ----------
    def _run(self):
        beta = 1.0 - (PARAMS["dt"] / PARAMS["tau_membrane"])   # 泄漏系数 / leak factor
        thr  = PARAMS["threshold_level"]                       # 阈值 / threshold
        dt_s = PARAMS["dt"] / 1000.0                           # 秒 / seconds

        step = 0
        while self.running:
            # 读取最新光强 / read latest light values
            with self._in_lock:
                left, right = self._left, self._right

            # 光强 → 泊松放电率 (10~200 Hz)
            # light → Poisson rate (10~200 Hz)
            hz_l = 10.0 + 190.0 * left
            hz_r = 10.0 + 190.0 * right
            p_l  = 1.0 - math.exp(-hz_l * dt_s)   # 放电概率 / spike probability
            p_r  = 1.0 - math.exp(-hz_r * dt_s)

            # 生成泊松输入 / generate Poisson input
            inp = torch.zeros(self.n, device=DEVICE)
            if self.sugar_L:
                inp[self.sugar_L] = (torch.rand(len(self.sugar_L)) < p_l).float() * 0.5
            if self.sugar_R:
                inp[self.sugar_R] = (torch.rand(len(self.sugar_R)) < p_r).float() * 0.5

            # 一步 LIF / one LIF step
            syn = self.weights @ self.spikes           # 突触输入 / synaptic input
            self.mem = beta * self.mem + syn + inp     # 膜电位更新 / update membrane
            new_spk  = (self.mem >= thr).float()       # 放电 / fire
            self.mem = self.mem * (1.0 - new_spk)      # 复位 / reset
            self.spikes = new_spk

            # 读 MN9 / read MN9
            if self.mn9_idx is not None:
                mn9 = self.spikes[self.mn9_idx].item()
            else:
                # 无 MN9 ID 时用整体活跃度近似 / fallback: overall activity
                mn9 = self.spikes.sum().item() / max(1, self.n) * 1000

            # 滑动平均 / running average
            with self._out_lock:
                self._mn9_rate = 0.95 * self._mn9_rate + 0.05 * mn9

            step += 1
            # 每 20 步打印一次 / print every 20 steps
            if step % 20 == 0:
                print(f"[Brain] step={step:5d} 活跃 / active={int(self.spikes.sum()):5d} "
                      f"L={left:.2f} R={right:.2f} MN9={self._mn9_rate:.3f}")

            # 控制仿真节奏 / throttle simulation speed
            time.sleep(1.0 / BRAIN_HZ)


# ============================================================
#              ROS 控制节点 / ROS control node
# ============================================================

class FlyTaxisNode(Node):
    """
    趋光控制节点：读光强、注入大脑、解码转向。
    Phototaxis controller: read light, inject into brain, decode steering.
    """

    def __init__(self, brain):
        super().__init__("fly_taxis")
        self.brain = brain

        self.fly_pose   = None    # 果蝇位姿 / fly pose
        self.light_pose = None    # 光源位姿 / light pose
        self.angular_filt = 0.0   # 角速度平滑 / angular velocity filter

        # 发布者与订阅者 / publishers and subscribers
        self.cmd_pub   = self.create_publisher(Twist,  "/fly/cmd_vel",  10)
        self.fly_sub   = self.create_subscription(Pose2D, "/fly/pose",   self.fly_cb,   10)
        self.light_sub = self.create_subscription(Pose2D, "/light/pose", self.light_cb, 10)

        # 控制循环定时器 / control loop timer
        self.create_timer(1.0 / CTRL_HZ, self.control_loop)

        self.get_logger().info(
            f"fly_taxis 启动 / started | 控制 / ctrl={CTRL_HZ}Hz 大脑 / brain={BRAIN_HZ}Hz")

    def fly_cb(self, msg):   self.fly_pose = msg      # 果蝇位姿回调 / fly pose callback
    def light_cb(self, msg): self.light_pose = msg    # 光源位姿回调 / light pose callback

    def control_loop(self):
        """主控制循环 / main control loop."""
        # 数据未就绪则跳过 / skip if data not ready
        if self.fly_pose is None or self.light_pose is None:
            return

        # 光源相对果蝇的向量（pygame 像素坐标, y 向下）
        # light vector relative to fly (pygame pixel coords, y downward)
        dx = self.light_pose.x - self.fly_pose.x
        dy = self.light_pose.y - self.fly_pose.y
        dist = math.hypot(dx, dy)         # 距离 / distance

        # 目标朝向 / target heading
        target = math.atan2(dy, dx)
        # 朝向误差，归一化到 [-π, π] / heading error, normalized to [-π, π]
        err = target - self.fly_pose.theta
        err = math.atan2(math.sin(err), math.cos(err))

        # 距离衰减 / distance attenuation
        raw = 1.0 / (1.0 + dist * 0.003)
        # 误差 → 左右光强 / error → left/right light
        left_light  = raw * (0.5 + 0.5 * math.sin(err))
        right_light = raw * (0.5 - 0.5 * math.sin(err))
        # 钳制到 [0, 1] / clamp to [0, 1]
        left_light  = max(0.0, min(1.0, left_light))
        right_light = max(0.0, min(1.0, right_light))

        # 注入大脑 / inject into brain
        self.brain.set_light(left_light, right_light)

        # 解码 / decoding
        mn9 = self.brain.get_mn9_rate()

        # err > 0 → 目标在左 → 左转（角速度正）
        # err > 0 → target on left → turn left (positive angular)
        angular_raw = err * TURN_GAIN * (0.5 + mn9)
        # 低通滤波 / low-pass filter
        self.angular_filt = 0.7 * self.angular_filt + 0.3 * angular_raw
        # 死区 / dead zone
        angular = 0.0 if abs(self.angular_filt) < 0.15 else self.angular_filt

        # 大误差时减速 / slow down on large error
        linear = SLOW_SPEED if abs(err) > SLOW_ANGLE_THR else BASE_SPEED

        # 发布速度指令 / publish velocity command
        cmd = Twist()
        cmd.linear.x  = float(linear)
        cmd.angular.z = float(angular)
        self.cmd_pub.publish(cmd)

        # 日志（每秒 1 次）/ log (once per second)
        self.get_logger().info(
            f"dist={dist:.0f} err={err:+.2f} | "
            f"L={left_light:.2f} R={right_light:.2f} MN9={mn9:.3f} | "
            f"v={linear:.2f} w={angular:+.2f}",
            throttle_duration_sec=1.0)


# ============================================================
#              主程序 / Main program
# ============================================================

def main():
    print("=" * 60)
    print("  Fly Brain — Phototaxis in ROS 2")
    print("=" * 60)

    # 检查神经元列表 / check neuron list
    if not CONFIG["path_completeness"].exists():
        print(f"\n❌ 找不到 / not found: {CONFIG['path_completeness']}")
        print("请从 / download from https://github.com/philshiu/Drosophila_brain_model:")
        print("  - 2023_03_23_completeness_630_final.csv")
        print("  - 2023_03_23_connectivity_630_final.parquet")
        return

    # 1. 神经元列表 / neuron list
    print("\n[1/3] 加载神经元列表 / loading neuron list...")
    neurons_list = pd.read_csv(CONFIG["path_completeness"], index_col=0)
    print(f"      {len(neurons_list)} 神经元 / neurons")

    # 2. 权重矩阵 / weight matrix
    weights = None
    if LOAD_WEIGHTS_FROM_DISK and CONFIG["path_saved_weights"].exists():
        print(f"\n[2/3] 从硬盘加载权重 / loading weights from disk...")
        try:
            weights = torch.load(CONFIG["path_saved_weights"],
                                 map_location="cpu").to_sparse_csr()
            print(f"      OK {weights.shape}")
        except Exception as e:
            print(f"      失败 / failed: {e}")
            weights = None

    if weights is None:
        print("\n[2/3] 加载连接数据 / loading connectivity...")
        conn = pd.read_parquet(CONFIG["path_connectivity"])
        print(f"      {len(conn)} 突触 / synapses")
        print("\n[3/3] 构建权重矩阵 / building weight matrix...")
        weights = build_weights_matrix(
            conn, len(neurons_list),
            PARAMS["weight_per_synapse"]
        ).to_sparse_csr()
        if SAVE_WEIGHTS_TO_DISK:
            CONFIG["path_saved_weights"].parent.mkdir(parents=True, exist_ok=True)
            torch.save(weights, CONFIG["path_saved_weights"])

    # 3. 启动大脑 / start brain
    print("\n[Brain] 初始化 / initializing...")
    brain = BrainSimulator(neurons_list, weights)

    # 4. 启动 ROS / start ROS
    print("\n[ROS] 启动节点 / starting node...")
    rclpy.init()
    node = FlyTaxisNode(brain)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\n用户中断 / user interrupt")
    finally:
        brain.stop()
        node.destroy_node()
        rclpy.shutdown()
        print("已停止 / stopped")


if __name__ == "__main__":
    main()
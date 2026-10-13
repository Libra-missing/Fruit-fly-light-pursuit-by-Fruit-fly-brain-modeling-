#!/usr/bin/env python3
"""
fly_brain_ros.py — 全脑模型 + 趋光控制 (单文件)

订阅:
  /fly/pose    (Pose2D, pygame 像素, y 向下)
  /light/pose  (Pose2D)
发布:
  /fly/cmd_vel (Twist)

大脑在独立线程中异步仿真。
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
#                      配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG = {
    "path_completeness": BASE_DIR / "data" / "2023_03_23_completeness_630_final.csv",
    "path_connectivity": BASE_DIR / "data" / "2023_03_23_connectivity_630_final.parquet",
    "path_saved_weights": BASE_DIR / "model" / "fly_brain_connections_weights.pth",
}

PARAMS = {
    "dt": 1.0,
    "tau_membrane": 20.0,
    "threshold_level": 1.0,
    "reset_level": 0.0,
    "weight_per_synapse": 0.05,
}

LOAD_WEIGHTS_FROM_DISK = True
SAVE_WEIGHTS_TO_DISK   = False

# MN9 (CB0701) — Virtual Fly Brain
MN9_FLYWIRE_ID = 720575940630329820

# 糖感觉神经元 (右侧 FlyWire v630, 来自 philshiu/Drosophila_brain_model)
SUGAR_GRN_IDS = [
    720575940624963786, 720575940630233916, 720575940637568838,
    720575940638202345, 720575940617000768, 720575940630797113,
    720575940632889389, 720575940621754367, 720575940621502051,
    720575940640649691, 720575940639332736, 720575940616885538,
    720575940639198653, 720575940620900446, 720575940617937543,
    720575940632425919, 720575940633143833, 720575940612670570,
    720575940628853239, 720575940629176663, 720575940611875570,
]

# 控制参数
CTRL_HZ        = 10
BRAIN_HZ       = 2
TURN_GAIN      = 3.5
BASE_SPEED     = 1.2
SLOW_SPEED     = 0.3
SLOW_ANGLE_THR = 0.5

DEVICE = torch.device("cpu")


# ============================================================
#              权重矩阵
# ============================================================

def build_weights_matrix(conn, n, w_syn):
    begin = datetime.datetime.now()
    print(f"[Weights] 开始构建 {begin}")

    pre  = torch.tensor(conn["Presynaptic_Index"].values,  dtype=torch.long, device=DEVICE)
    post = torch.tensor(conn["Postsynaptic_Index"].values, dtype=torch.long, device=DEVICE)
    w    = torch.tensor(conn["Excitatory x Connectivity"].values,
                        device=DEVICE) * w_syn

    indices = torch.stack([post, pre])
    mat = torch.sparse_coo_tensor(
        indices, w,
        size=(n, n), device=DEVICE,
    ).coalesce()

    print(f"[Weights] 完成, 耗时 {datetime.datetime.now() - begin}")
    return mat


# ============================================================
#              大脑仿真器 (后台线程)
# ============================================================

class BrainSimulator:
    def __init__(self, neurons_list, weights):
        self.neurons_list = neurons_list
        self.weights = weights
        self.n = len(neurons_list)

        self.sugar_idx = [
            neurons_list.index.get_loc(nid)
            for nid in SUGAR_GRN_IDS
            if nid in neurons_list.index
        ]
        half = max(1, len(self.sugar_idx) // 2)
        self.sugar_L = self.sugar_idx[:half]
        self.sugar_R = self.sugar_idx[half:]
        print(f"[Brain] 糖 GRN: {len(self.sugar_idx)}/{len(SUGAR_GRN_IDS)} 匹配 "
              f"(L={len(self.sugar_L)} R={len(self.sugar_R)})")

        if MN9_FLYWIRE_ID in neurons_list.index:
            self.mn9_idx = neurons_list.index.get_loc(MN9_FLYWIRE_ID)
            print(f"[Brain] MN9 索引: {self.mn9_idx}")
        else:
            self.mn9_idx = None
            print(f"[Brain] ⚠ MN9 {MN9_FLYWIRE_ID} 不在列表中，用近似替代")

        self.mem    = torch.zeros(self.n, device=DEVICE)
        self.spikes = torch.zeros(self.n, device=DEVICE)

        self._left = 0.0
        self._right = 0.0
        self._in_lock = threading.Lock()

        self._mn9_rate = 0.0
        self._out_lock = threading.Lock()

        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print("[Brain] 仿真线程已启动")

    def set_light(self, l, r):
        with self._in_lock:
            self._left, self._right = float(l), float(r)

    def get_mn9_rate(self):
        with self._out_lock:
            return self._mn9_rate

    def stop(self):
        self.running = False
        self.thread.join(timeout=2.0)

    def _run(self):
        beta = 1.0 - (PARAMS["dt"] / PARAMS["tau_membrane"])
        thr  = PARAMS["threshold_level"]
        dt_s = PARAMS["dt"] / 1000.0

        step = 0
        while self.running:
            with self._in_lock:
                left, right = self._left, self._right

            # 光强 → 泊松放电率 (10~200 Hz)
            hz_l = 10.0 + 190.0 * left
            hz_r = 10.0 + 190.0 * right
            p_l  = 1.0 - math.exp(-hz_l * dt_s)
            p_r  = 1.0 - math.exp(-hz_r * dt_s)

            inp = torch.zeros(self.n, device=DEVICE)
            if self.sugar_L:
                inp[self.sugar_L] = (torch.rand(len(self.sugar_L)) < p_l).float() * 0.5
            if self.sugar_R:
                inp[self.sugar_R] = (torch.rand(len(self.sugar_R)) < p_r).float() * 0.5

            # 一步 LIF
            syn = self.weights @ self.spikes
            self.mem = beta * self.mem + syn + inp
            new_spk  = (self.mem >= thr).float()
            self.mem = self.mem * (1.0 - new_spk)
            self.spikes = new_spk

            # 读 MN9（若无 ID，用糖 GRN 下游的活跃度近似）
            if self.mn9_idx is not None:
                mn9 = self.spikes[self.mn9_idx].item()
            else:
                mn9 = self.spikes.sum().item() / max(1, self.n) * 1000

            with self._out_lock:
                self._mn9_rate = 0.95 * self._mn9_rate + 0.05 * mn9

            step += 1
            if step % 20 == 0:
                print(f"[Brain] step={step:5d} 活跃={int(self.spikes.sum()):5d} "
                      f"L={left:.2f} R={right:.2f} MN9={self._mn9_rate:.3f}")

            time.sleep(1.0 / BRAIN_HZ)


# ============================================================
#              ROS 控制节点
# ============================================================

class FlyTaxisNode(Node):
    def __init__(self, brain):
        super().__init__("fly_taxis")
        self.brain = brain

        self.fly_pose   = None
        self.light_pose = None
        self.angular_filt = 0.0

        self.cmd_pub   = self.create_publisher(Twist,  "/fly/cmd_vel",  10)
        self.fly_sub   = self.create_subscription(Pose2D, "/fly/pose",   self.fly_cb,   10)
        self.light_sub = self.create_subscription(Pose2D, "/light/pose", self.light_cb, 10)

        self.create_timer(1.0 / CTRL_HZ, self.control_loop)

        self.get_logger().info(
            f"fly_taxis 启动 | 控制={CTRL_HZ}Hz 大脑={BRAIN_HZ}Hz")

    def fly_cb(self, msg):   self.fly_pose = msg
    def light_cb(self, msg): self.light_pose = msg

    def control_loop(self):
        if self.fly_pose is None or self.light_pose is None:
            return

        # 光源相对果蝇的向量（pygame 像素坐标, y 向下）
        dx = self.light_pose.x - self.fly_pose.x
        dy = self.light_pose.y - self.fly_pose.y
        dist = math.hypot(dx, dy)

        # 目标朝向
        target = math.atan2(dy, dx)
        err = target - self.fly_pose.theta
        err = math.atan2(math.sin(err), math.cos(err))

        # 距离衰减
        raw = 1.0 / (1.0 + dist * 0.003)
        left_light  = raw * (0.5 + 0.5 * math.sin(err))
        right_light = raw * (0.5 - 0.5 * math.sin(err))
        left_light  = max(0.0, min(1.0, left_light))
        right_light = max(0.0, min(1.0, right_light))

        # 注入大脑
        self.brain.set_light(left_light, right_light)

        # 解码
        mn9 = self.brain.get_mn9_rate()

        # err > 0 → 目标在左 → 左转(角速度正)
        angular_raw = err * TURN_GAIN * (0.5 + mn9)
        self.angular_filt = 0.7 * self.angular_filt + 0.3 * angular_raw
        angular = 0.0 if abs(self.angular_filt) < 0.15 else self.angular_filt

        linear = SLOW_SPEED if abs(err) > SLOW_ANGLE_THR else BASE_SPEED

        cmd = Twist()
        cmd.linear.x  = float(linear)
        cmd.angular.z = float(angular)
        self.cmd_pub.publish(cmd)

        self.get_logger().info(
            f"dist={dist:.0f} err={err:+.2f} | "
            f"L={left_light:.2f} R={right_light:.2f} MN9={mn9:.3f} | "
            f"v={linear:.2f} w={angular:+.2f}",
            throttle_duration_sec=1.0)


# ============================================================
#              主程序
# ============================================================

def main():
    print("=" * 60)
    print("  Fly Brain — Phototaxis in ROS 2")
    print("=" * 60)

    if not CONFIG["path_completeness"].exists():
        print(f"\n❌ 找不到 {CONFIG['path_completeness']}")
        print("请从 https://github.com/philshiu/Drosophila_brain_model 下载:")
        print("  - 2023_03_23_completeness_630_final.csv")
        print("  - 2023_03_23_connectivity_630_final.parquet")
        return

    # 1. 神经元列表
    print("\n[1/3] 加载神经元列表...")
    neurons_list = pd.read_csv(CONFIG["path_completeness"], index_col=0)
    print(f"      {len(neurons_list)} 神经元")

    # 2. 权重
    weights = None
    if LOAD_WEIGHTS_FROM_DISK and CONFIG["path_saved_weights"].exists():
        print(f"\n[2/3] 从硬盘加载权重...")
        try:
            weights = torch.load(CONFIG["path_saved_weights"],
                                 map_location="cpu").to_sparse_csr()
            print(f"      OK {weights.shape}")
        except Exception as e:
            print(f"      失败: {e}")
            weights = None

    if weights is None:
        print("\n[2/3] 加载连接数据...")
        conn = pd.read_parquet(CONFIG["path_connectivity"])
        print(f"      {len(conn)} 突触")
        print("\n[3/3] 构建权重矩阵...")
        weights = build_weights_matrix(
            conn, len(neurons_list),
            PARAMS["weight_per_synapse"]
        ).to_sparse_csr()
        if SAVE_WEIGHTS_TO_DISK:
            CONFIG["path_saved_weights"].parent.mkdir(parents=True, exist_ok=True)
            torch.save(weights, CONFIG["path_saved_weights"])

    # 3. 大脑
    print("\n[Brain] 初始化...")
    brain = BrainSimulator(neurons_list, weights)

    # 4. ROS
    print("\n[ROS] 启动节点...")
    rclpy.init()
    node = FlyTaxisNode(brain)
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        brain.stop()
        node.destroy_node()
        rclpy.shutdown()
        print("已停止")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""fly_env.py — pygame 2D 环境，ROS 2 节点。

话题:
  Sub: /fly/cmd_vel    (geometry_msgs/Twist)
  Pub: /fly/pose       (geometry_msgs/Pose2D)   pygame 像素坐标, y 向下
       /light/pose     (geometry_msgs/Pose2D)   光源位置
"""

import math
import threading
from pathlib import Path

import pygame
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, Pose2D

# ---------- 场景参数 ----------
WIN_W, WIN_H   = 800, 600
FPS            = 30

FLY_START_X    = 400.0
FLY_START_Y    = 500.0
FLY_START_TH   = -math.pi / 2      # 朝上
FLY_SPEED_PX   = 80.0              # cmd_vel.linear.x=1.0 → 80 px/s
FLY_MARGIN     = 20

LIGHT_CX       = WIN_W / 2.0       # 绕圈中心
LIGHT_CY       = WIN_H / 2.0
LIGHT_R        = 200.0             # 绕圈半径 (px)
LIGHT_OMEGA    = 0.6               # rad/s
# --------------------------------


class FlyEnv(Node):
    def __init__(self):
        super().__init__("fly_env")

        self.pose_pub  = self.create_publisher(Pose2D, "/fly/pose",   10)
        self.light_pub = self.create_publisher(Pose2D, "/light/pose", 10)
        self.cmd_sub   = self.create_subscription(
            Twist, "/fly/cmd_vel", self.cmd_cb, 10)

        # 状态
        self.fly_x     = FLY_START_X
        self.fly_y     = FLY_START_Y
        self.fly_theta = FLY_START_TH
        self.light_ang = 0.0

        self._cmd_v     = 0.0
        self._cmd_omega = 0.0
        self._lock = threading.Lock()

        self.running = True

        # 后台线程跑 pygame
        self.thread = threading.Thread(target=self._pygame_loop, daemon=True)
        self.thread.start()

        # ROS 定时器发布 pose
        self.create_timer(1.0 / FPS, self._publish)

        self.get_logger().info("fly_env 启动，窗口打开中...")

    # ---------- ROS ----------
    def cmd_cb(self, msg: Twist):
        with self._lock:
            self._cmd_v     = msg.linear.x
            self._cmd_omega = msg.angular.z

    def _publish(self):
        lx = LIGHT_CX + LIGHT_R * math.cos(self.light_ang)
        ly = LIGHT_CY + LIGHT_R * math.sin(self.light_ang)

        self.pose_pub.publish(Pose2D(
            x=float(self.fly_x), y=float(self.fly_y),
            theta=float(self.fly_theta)))

        self.light_pub.publish(Pose2D(
            x=float(lx), y=float(ly), theta=0.0))

    # ---------- pygame ----------
    def _pygame_loop(self):
        pygame.init()
        screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption("Fly Brain — Phototaxis")
        clock = pygame.time.Clock()

        assets = Path(__file__).resolve().parent / "assets"
        grass = pygame.image.load(str(assets / "grass.png")).convert()
        fly   = pygame.image.load(str(assets / "fly.png")).convert_alpha()
        light = pygame.image.load(str(assets / "light.png")).convert_alpha()

        font = pygame.font.SysFont(None, 22)
        dt = 1.0 / FPS

        while self.running:
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.running = False

            # --- 物理 ---
            with self._lock:
                v, omega = self._cmd_v, self._cmd_omega

            self.fly_theta += omega * dt
            self.fly_x     += v * FLY_SPEED_PX * math.cos(self.fly_theta) * dt
            self.fly_y     += v * FLY_SPEED_PX * math.sin(self.fly_theta) * dt

            # 边界
            self.fly_x = max(FLY_MARGIN, min(WIN_W - FLY_MARGIN, self.fly_x))
            self.fly_y = max(FLY_MARGIN, min(WIN_H - FLY_MARGIN, self.fly_y))

            # 光源绕圈
            self.light_ang += LIGHT_OMEGA * dt
            lx = LIGHT_CX + LIGHT_R * math.cos(self.light_ang)
            ly = LIGHT_CY + LIGHT_R * math.sin(self.light_ang)

            # --- 渲染 ---
            screen.blit(grass, (0, 0))

            # 光源（缓慢自转的射线）
            light_rot = pygame.transform.rotate(
                light, -math.degrees(self.light_ang * 3))
            screen.blit(light_rot,
                        light_rot.get_rect(center=(int(lx), int(ly))))

            # 果蝇
            # 图片默认朝上; theta=0 表示朝右
            rot_deg = -math.degrees(self.fly_theta) - 90
            fly_rot = pygame.transform.rotate(fly, rot_deg)
            screen.blit(fly_rot,
                        fly_rot.get_rect(center=(int(self.fly_x),
                                                 int(self.fly_y))))

            # HUD
            txt = font.render(
                f"Fly ({self.fly_x:.0f},{self.fly_y:.0f})  "
                f"theta={math.degrees(self.fly_theta):+.0f}deg  "
                f"v={v:+.2f} w={omega:+.2f}",
                True, (255, 255, 255))
            screen.blit(txt, (10, 10))
            screen.blit(font.render("Light orbits clockwise",
                                    True, (255, 255, 200)), (10, 32))

            pygame.display.flip()
            clock.tick(FPS)

        pygame.quit()

    def stop(self):
        self.running = False
        self.thread.join(timeout=2.0)


def main():
    rclpy.init()
    node = FlyEnv()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
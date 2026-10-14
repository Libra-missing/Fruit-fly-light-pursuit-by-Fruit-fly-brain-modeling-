#!/usr/bin/env python3
"""
fly_env.py — pygame 2D 环境，ROS 2 节点
fly_env.py — pygame 2D environment, ROS 2 node

话题 / Topics:
  Sub: /fly/cmd_vel    (geometry_msgs/Twist)
  Pub: /fly/pose       (geometry_msgs/Pose2D)  pygame 像素坐标, y 向下
                                               pygame pixel coords, y downward
       /light/pose     (geometry_msgs/Pose2D)  光源位置 / light position
"""

import math
import threading
from pathlib import Path

import pygame
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, Pose2D

# ---------- 场景参数 / Scene parameters ----------
WIN_W, WIN_H   = 800, 600           # 窗口尺寸 / window size
FPS            = 30                 # 渲染帧率 / render FPS

FLY_START_X    = 400.0              # 果蝇初始 X / fly initial X
FLY_START_Y    = 500.0              # 果蝇初始 Y / fly initial Y
FLY_START_TH   = -math.pi / 2       # 果蝇初始朝向（朝上）/ initial heading (up)
FLY_SPEED_PX   = 80.0               # cmd_vel.linear.x=1.0 时的像素速度
                                    # pixels/s at cmd_vel.linear.x = 1.0
FLY_MARGIN     = 20                 # 边界留白 / boundary margin

LIGHT_CX       = WIN_W / 2.0        # 光源绕圈中心 X / orbit center X
LIGHT_CY       = WIN_H / 2.0        # 光源绕圈中心 Y / orbit center Y
LIGHT_R        = 200.0              # 绕圈半径（像素）/ orbit radius (px)
LIGHT_OMEGA    = 0.6                # 绕圈角速度（rad/s）/ orbit angular speed
# ----------------------------------------------------


class FlyEnv(Node):
    """
    ROS 2 节点：维护 2D 世界，发布果蝇与光源位姿，接收速度指令。
    ROS 2 node: maintains the 2D world, publishes fly and light poses,
    subscribes to velocity commands.
    """

    def __init__(self):
        super().__init__("fly_env")

        # 发布者与订阅者 / Publishers and subscribers
        self.pose_pub  = self.create_publisher(Pose2D, "/fly/pose",   10)
        self.light_pub = self.create_publisher(Pose2D, "/light/pose", 10)
        self.cmd_sub   = self.create_subscription(
            Twist, "/fly/cmd_vel", self.cmd_cb, 10)

        # 状态 / State
        self.fly_x     = FLY_START_X        # 果蝇 X 坐标 / fly X coordinate
        self.fly_y     = FLY_START_Y        # 果蝇 Y 坐标 / fly Y coordinate
        self.fly_theta = FLY_START_TH       # 果蝇朝向（弧度）/ fly heading (rad)
        self.light_ang = 0.0                # 光源绕圈角 / light orbit angle

        # 速度指令缓存（线程安全）/ cached velocity command (thread-safe)
        self._cmd_v     = 0.0               # 线速度 / linear velocity
        self._cmd_omega = 0.0               # 角速度 / angular velocity
        self._lock = threading.Lock()

        self.running = True

        # 后台线程跑 pygame / run pygame in a background thread
        self.thread = threading.Thread(target=self._pygame_loop, daemon=True)
        self.thread.start()

        # ROS 定时器发布 pose / ROS timer to publish pose
        self.create_timer(1.0 / FPS, self._publish)

        self.get_logger().info("fly_env 启动，窗口打开中... / starting, opening window...")

    # ---------- ROS 回调 / ROS callbacks ----------
    def cmd_cb(self, msg: Twist):
        """接收速度指令 / receive velocity command."""
        with self._lock:
            self._cmd_v     = msg.linear.x
            self._cmd_omega = msg.angular.z

    def _publish(self):
        """发布果蝇与光源位姿 / publish fly and light poses."""
        # 计算当前光源位置 / compute current light position
        lx = LIGHT_CX + LIGHT_R * math.cos(self.light_ang)
        ly = LIGHT_CY + LIGHT_R * math.sin(self.light_ang)

        # 发布果蝇位姿 / publish fly pose
        self.pose_pub.publish(Pose2D(
            x=float(self.fly_x), y=float(self.fly_y),
            theta=float(self.fly_theta)))

        # 发布光源位姿 / publish light pose
        self.light_pub.publish(Pose2D(
            x=float(lx), y=float(ly), theta=0.0))

    # ---------- pygame 主循环 / pygame main loop ----------
    def _pygame_loop(self):
        """pygame 渲染与物理循环 / pygame render and physics loop."""
        pygame.init()
        screen = pygame.display.set_mode((WIN_W, WIN_H))
        pygame.display.set_caption("Fly Brain — Phototaxis")
        clock = pygame.time.Clock()

        # 加载素材 / load assets
        assets = Path(__file__).resolve().parent / "assets"
        grass = pygame.image.load(str(assets / "grass.png")).convert()
        fly   = pygame.image.load(str(assets / "fly.png")).convert_alpha()
        light = pygame.image.load(str(assets / "light.png")).convert_alpha()

        font = pygame.font.SysFont(None, 22)
        dt = 1.0 / FPS      # 每帧时间步 / time step per frame

        while self.running:
            # 处理退出事件 / handle quit event
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.running = False

            # --- 物理更新 / physics update ---
            with self._lock:
                v, omega = self._cmd_v, self._cmd_omega

            # 更新朝向与位置（差速模型）/ update heading and position
            self.fly_theta += omega * dt
            self.fly_x     += v * FLY_SPEED_PX * math.cos(self.fly_theta) * dt
            self.fly_y     += v * FLY_SPEED_PX * math.sin(self.fly_theta) * dt

            # 边界钳制 / clamp to window
            self.fly_x = max(FLY_MARGIN, min(WIN_W - FLY_MARGIN, self.fly_x))
            self.fly_y = max(FLY_MARGIN, min(WIN_H - FLY_MARGIN, self.fly_y))

            # 光源绕圈 / light orbits around center
            self.light_ang += LIGHT_OMEGA * dt
            lx = LIGHT_CX + LIGHT_R * math.cos(self.light_ang)
            ly = LIGHT_CY + LIGHT_R * math.sin(self.light_ang)

            # --- 渲染 / rendering ---
            screen.blit(grass, (0, 0))

            # 光源（缓慢自转的射线）/ light with slowly rotating rays
            light_rot = pygame.transform.rotate(
                light, -math.degrees(self.light_ang * 3))
            screen.blit(light_rot,
                        light_rot.get_rect(center=(int(lx), int(ly))))

            # 果蝇（图片默认朝上，theta=0 表示朝右）
            # fly (sprite faces up, theta=0 means facing right)
            rot_deg = -math.degrees(self.fly_theta) - 90
            fly_rot = pygame.transform.rotate(fly, rot_deg)
            screen.blit(fly_rot,
                        fly_rot.get_rect(center=(int(self.fly_x),
                                                 int(self.fly_y))))

            # HUD 文本 / HUD text
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
        """停止渲染线程 / stop the render thread."""
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
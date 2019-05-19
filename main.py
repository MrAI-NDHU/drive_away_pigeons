from queue import Queue
from threading import Thread
from typing import Any
import logging
import math
import numpy
import os
import pickle
import random
import select
import sys
import time

from Jetson import GPIO
import cv2
import keyboard

from servo import Servo
from servo.controller import ControllerForPCA9685
import darknet

Y, X = "y", "x"
Y1, X1, Y2, X2, CX, CY, A, R = "y1", "x1", "y1", "x1", "cx", "cy", "a", "r"
N, D, E = 0, 1, 2


class DriveAwayPigeons:
    
    def __init__(self, split_w: int, split_h: int, angle_prec: float):
        self.split_w, self.split_h = split_w, split_h
        self.areas_cnt = self.split_w * self.split_h
        self.angle_prec = angle_prec
        self.laser_pin = 18
        self.sweep_limit_times = 4
        self.detect_error_times = 4
        self.can_detect, self.can_sweep = False, False
        self.cap = cv2.VideoCapture(0)
        self.cap_ratio = 16 / 9
        self.font = cv2.FONT_HERSHEY_DUPLEX
        self.showing_w, self.showing_h = 1280, 720
        self.area_normal_color = (0x00, 0xFF, 0x00)  # green
        self.area_detected_color = (0xFF, 0x00, 0x00)  # blue
        self.area_error_color = (0x00, 0x00, 0xFF)  # red
        self.detection_color = (0xFF, 0xFF, 0x00)  # cyan
        self.area_canter_color = (0xFF, 0x00, 0xFF)  # purple
        self.others_color = (0x00, 0xFF, 0xFF)  # yellow
        
        self.areas_rect = self.set_areas_rect()
        self.areas_status = [[N] * self.split_w for _ in range(self.split_h)]
        self.areas_error = [[0] * self.split_w for _ in range(self.split_h)]
        self.arm = self.set_arm()
        self.init_laser()
        
        self.thd_detecting = Thread(target=self.thd_detecting_func)
        self.thd_deciding = Thread(target=self.thd_deciding_func)
        self.thd_sweeping = Thread(target=self.thd_sweeping_func)
        self.thd_showing = Thread(target=self.thd_showing_func)
        self.que_deciding = Queue(1)
        self.que_sweeping = Queue(1)
        self.que_showing = Queue(1)
        self.thd_showing.start()
        
        self.areas_angle = [[{}]]
        self.area_angle_spacing = {}
        self.init_areas_angle()
        
        self.darknet_net, self.darknet_net_w, self.darknet_net_h = None, 0, 0
        self.darknet_meta, self.darknet_img = None, None
        self.init_darknet()
        
        self.thd_detecting.start()
        self.thd_deciding.start()
        self.thd_sweeping.start()
        
        self.thd_detecting.join()
    
    def __del__(self):
        GPIO.cleanup()
    
    def init_darknet(self):
        config_path = "./cfg/yolov3-tiny.cfg"
        weight_path = "./yolov3-tiny_pigeon.weights"
        meta_path = "./cfg/pigeon.data"
        self.darknet_net = darknet.load_net_custom(config_path.encode(
            "ascii"), weight_path.encode("ascii"), 0, 1)
        self.darknet_net_w = darknet.network_width(self.darknet_net)
        self.darknet_net_h = darknet.network_height(self.darknet_net)
        self.darknet_meta = darknet.load_meta(meta_path.encode("ascii"))
        self.darknet_img = darknet.make_image(
            self.darknet_net_w, self.darknet_net_h, 3)
    
    def init_laser(self):
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(self.laser_pin, GPIO.OUT, initial=GPIO.LOW)
        if TEST_MODE:
            logging.info("init_laser: test start")
            for i in range(0):
                time.sleep(2)
                self.open_laser()
                time.sleep(2)
                self.close_laser()
            logging.info("init_laser: test finish")
    
    def open_laser(self):
        GPIO.output(self.laser_pin, GPIO.HIGH)
    
    def close_laser(self):
        GPIO.output(self.laser_pin, GPIO.LOW)
    
    def set_arm(self) -> ControllerForPCA9685:
        mg995_sec_per_angle = \
            ((0.16 - 0.2) / (6.0 - 4.8) * (5.0 - 4.8) + 0.2) / 60.0
        mg995_tilt = Servo(0.0, 180.0, 150.0, 510.0, 50.0, mg995_sec_per_angle)
        mg995_pan = Servo(0.0, 180.0, 180.0, 630.0, 50.0, mg995_sec_per_angle)
        servos = {Y: mg995_tilt, X: mg995_pan}
        chs = {Y: 0, X: 1}
        return ControllerForPCA9685(servos, chs, 60.0)
    
    def set_areas_rect(self) -> [[{}]]:
        areas_rect = [[None] * self.split_w for _ in range(self.split_h)]
        aw, ah = self.showing_w / self.split_w, self.showing_h / self.split_h
        for ay in range(self.split_h):
            for ax in range(self.split_w):
                x1, y1 = aw * ax, aw * ay
                x2, y2 = aw * (ax + 1), aw * (ay + 1)
                areas_rect[ay][ax][X1] = int(round(x1))
                areas_rect[ay][ax][Y1] = int(round(y1))
                areas_rect[ay][ax][X2] = int(round(x2))
                areas_rect[ay][ax][Y2] = int(round(y2))
                areas_rect[ay][ax][CX] = (x1 + x2) / 2
                areas_rect[ay][ax][CY] = (y1 + y2) / 2
        return areas_rect
    
    def check_areas_angle(self):
        for ay in range(self.split_h):
            for ax in range(self.split_w):
                if self.areas_angle[ay][ax] is None:
                    raise Exception("failed to check areas angle")
                if TEST_MODE:
                    n = ay * self.split_w + ax
                    logging.info("check_areas_angle: check {}: {}"
                                 .format(n, self.areas_angle[ay][ax]))
                    self.arm.rotate(self.areas_angle[ay][ax], False)
                    self.open_laser()
                    time.sleep(2)
                    self.can_sweep = True
                    for i in range(100):
                        self.sweep_area(ax, ay)
                    self.can_sweep = False
                    self.close_laser()
    
    def init_areas_angle(self):
        self.areas_angle = [[None] * self.split_w for _ in range(self.split_h)]
        flush_stdin()
        filepath = input("Load areas angle: ").strip()
        if filepath != "":
            with open(filepath, "rb") as f:
                self.areas_angle = pickle.load(f)
            self.init_sweep_attrs()
            self.check_areas_angle()
        else:
            self.open_laser()
            while True:
                if keyboard.is_pressed("e"):
                    break
                elif keyboard.is_pressed("up"):
                    self.arm.rotate({Y: -self.angle_prec}, True)
                elif keyboard.is_pressed("down"):
                    self.arm.rotate({Y: self.angle_prec}, True)
                elif keyboard.is_pressed("left"):
                    self.arm.rotate({X: self.angle_prec}, True)
                elif keyboard.is_pressed("right"):
                    self.arm.rotate({X: -self.angle_prec}, True)
                elif keyboard.is_pressed("s"):
                    flush_stdin()
                    i = int(input("Input area number in range [{},{}]: "
                                  .format(0, self.split_w * self.split_h - 1)))
                    self.areas_angle[i // self.split_w][i % self.split_w] = \
                        self.arm.current_angles.copy()
            self.close_laser()
            self.init_sweep_attrs()
            self.check_areas_angle()
            flush_stdin()
            filepath = input("Save areas angle: ").strip()
            if filepath != "":
                with open(filepath, "wb") as f:
                    pickle.dump(self.areas_angle, f)
    
    def init_sweep_attrs(self):
        self.area_angle_spacing = {X: 0.0, Y: 0.0}
        for i in range(1, self.areas_cnt):
            ax1, ay1 = i % self.split_w, i // self.split_w
            ax0, ay0 = (i - 1) % self.split_w, (i - 1) // self.split_w
            if ax1 > ax0:
                area1_x = self.areas_angle[ay1][ax1][X]
                area0_x = self.areas_angle[ay0][ax0][X]
                self.area_angle_spacing[X] = max(
                    abs(area1_x - area0_x), self.area_angle_spacing[X])
            if ay1 > ay0:
                area1_y = self.areas_angle[ay1][ax1][Y]
                area0_y = self.areas_angle[ay0][ax0][Y]
                self.area_angle_spacing[Y] = max(
                    abs(area1_y - area0_y), self.area_angle_spacing[Y])
    
    def sweep_area(self, area_x: int, area_y: int):
        if not self.can_sweep:
            return
        area_angle = self.areas_angle[area_y][area_x]
        x = random.uniform(
            area_angle[X] - self.area_angle_spacing[X] / 2,
            area_angle[X] + self.area_angle_spacing[X] / 2)
        y = random.uniform(
            area_angle[Y] - self.area_angle_spacing[Y] / 2,
            area_angle[Y] + self.area_angle_spacing[Y] / 2)
        time.sleep(0.05)
        self.arm.rotate({X: x, Y: y}, False)
    
    def get_cap_img(self, w, h) -> numpy.ndarray:
        _, img = self.cap.read()
        return cv2.resize(img, (w, h),
                          interpolation=cv2.INTER_LINEAR)
    
    def draw_areas(self, img: numpy.ndarray, has_canter: bool = False):
        padding, size = 2, 20
        for ay in range(0, self.split_h):
            for ax in range(0, self.split_w):
                i_str = str(ay * self.split_w + ax)
                rect = self.areas_rect[ay][ax]
                if self.areas_status[ay][ax] == N:
                    color = self.area_normal_color
                elif self.areas_status[ay][ax] == D:
                    color = self.area_detected_color
                else:
                    color = self.area_error_color
                    str(self.areas_error[ay][ax])
                    cv2.putText(img, str(self.areas_error[ay][ax]),
                                (rect[X1] + padding,
                                 rect[Y2] - padding - size // 2),
                                self.font, 0.5, color, 1, cv2.LINE_AA)
                cv2.rectangle(img, (rect[X1], rect[Y1]),
                              (rect[X2], rect[Y2]), color, 1)
                cv2.putText(img, i_str, (rect[X1] + padding,
                                         rect[Y1] + padding + size),
                            self.font, 1, color, 1, cv2.LINE_AA)
                if has_canter:
                    c = (int(round(rect[CX])), int(round(rect[CY])))
                    cv2.circle(img, c, 4, self.area_canter_color, 1)
    
    def draw_fps(self, img: numpy.ndarray, fps: float):
        color, padding, width = self.others_color, 1, 50
        cv2.putText(img, "FPS:{:5.2f}".format(fps),
                    (self.showing_w - padding - width, 0 + padding),
                    self.font, 0.5, color, 1, cv2.LINE_AA)
    
    def draw_detections(self, img: numpy.ndarray, detections: [{}]):
        color, padding, size = self.detection_color, 1, 10
        for d in detections:
            cv2.rectangle(img, (d[X1], d[Y1]), (d[Y1], d[Y2]), color, 1)
            cv2.putText(img, "{:6.2f}".format(d[R]),
                        (d[X1] + padding, d[Y1] + padding + size),
                        self.font, 0.5, color, 1, cv2.LINE_AA)
    
    def fix_detections(self, detections: [[[Any]]]) -> [{}]:
        new_detections = [{} for _ in range(len(detections))]
        for i in range(len(detections)):
            d = detections[i]
            new_detections[i][R] = d[1]
            wr = self.showing_w / self.darknet_net_w
            hr = self.showing_h / self.darknet_net_h
            x, y, w, h = d[2][0] * wr, d[2][1] * hr, d[2][2] * wr, d[2][3] * hr
            new_detections[i][X1] = x - (w / 2)
            new_detections[i][Y1] = x + (w / 2)
            new_detections[i][X2] = y - (h / 2)
            new_detections[i][Y2] = y + (h / 2)
            dist = sys.maxsize
            for ay in range(0, self.split_h):
                for ax in range(0, self.split_w):
                    rect = self.areas_rect[ay][ax]
                    d2 = math.pow(rect[CX] - x, 2) + math.pow(rect[CY] - y, 2)
                    if d2 < dist:
                        new_detections[i][A] = ay * self.split_w + ax
                        dist = d2
        return new_detections
    
    def thd_detecting_func(self):
        self.can_detect = True
        while True:
            begin_time = time.time()
            img = self.get_cap_img(self.darknet_net_w, self.darknet_net_h)
            cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            darknet.copy_image_from_bytes(self.darknet_img, img.tobytes())
            detections = darknet.detect_image(self.darknet_net,
                                              self.darknet_meta,
                                              self.darknet_img, thresh=0.25)
            fps = 1 / (time.time() - begin_time)
            self.que_deciding.put((detections, fps))
    
    def thd_deciding_func(self):
        while True:
            detections, fps = self.que_deciding.get()
            detections = self.fix_detections(detections)
            
            # TODO
            # time.sleep(0.01)  # if high cpu usage
            area_x, area_y = None, None
            detected_areas, sweeping_area, ignored_areas, fps = \
                None, None, None, None
            self.que_sweeping.put((area_x, area_y))
            self.que_showing.put(
                (detected_areas, sweeping_area, ignored_areas, fps))
            pass
    
    def thd_sweeping_func(self):
        self.can_sweep = True
        while True:
            if not self.can_sweep:
                time.sleep(0.01)  # avoid high CPU usage
                continue
            area_x, area_y = self.que_sweeping.get()
            self.sweep_area(area_x, area_y)
    
    def thd_showing_func(self):
        # detected_areas, sweeping_area, ignored_areas, fps =
        while True:
            img = None
            if not self.can_detect:
                img = self.get_cap_img(self.showing_w, self.showing_h)
                self.draw_areas(img, True)
            else:
                detected_areas, sweeping_area, ignored_areas, fps = \
                    self.que_showing.get()
                # TODO
            cv2.imshow("image", img)
            cv2.waitKey(1)


TEST_MODE = True


def main():
    logging.basicConfig(stream=sys.stdout, level=logging.INFO)
    random.seed()
    
    split_w, split_h = 4, 3
    if len(sys.argv) == 2:
        split_w, split_h = int(sys.argv[1]), int(sys.argv[1])
    elif len(sys.argv) >= 3:
        split_w, split_h = int(sys.argv[1]), int(sys.argv[2])
    d = None
    try:
        d = DriveAwayPigeons(split_w, split_h, 0.05)
    except KeyboardInterrupt:
        del d


def flush_stdin():
    # termios.tcflush(sys.stdin, termios.TCIOFLUSH)
    while len(select.select([sys.stdin.fileno()], [], [], None)[0]) > 0:
        os.read(sys.stdin.fileno(), 4096)


if __name__ == '__main__':
    main()

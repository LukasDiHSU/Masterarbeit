#!/usr/bin/env python3
"""Compact Q1 sensor summaries for LLM agents.

Publishes std_msgs/String JSON on:
  /q1_sensor_summary/lidar
  /q1_sensor_summary/semantic_lidar
  /q1_sensor_summary/camera

Writes JPEG/PNG stills for the multimodal camera agent:
  AGENT_CAMERA_RGB_JPEG (default /tmp/q1_camera_rgb.jpg)
  AGENT_CAMERA_SEM_JPEG (default /tmp/q1_camera_semantic.jpg)
"""

from __future__ import annotations

import json
import math
import os
import struct
import time
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import rclpy
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import (
    HistoryPolicy,
    QoSProfile,
    ReliabilityPolicy,
    qos_profile_sensor_data,
)
from sensor_msgs.msg import Image, PointCloud2, PointField
from std_msgs.msg import String

from .goose_classes import class_id_from_rgb, class_name

try:
    from tf2_ros import Buffer, TransformListener
except Exception:  # pragma: no cover
    Buffer = None  # type: ignore
    TransformListener = None  # type: ignore

_DATATYPE = {
    PointField.INT8: ("b", 1),
    PointField.UINT8: ("B", 1),
    PointField.INT16: ("h", 2),
    PointField.UINT16: ("H", 2),
    PointField.INT32: ("i", 4),
    PointField.UINT32: ("I", 4),
    PointField.FLOAT32: ("f", 4),
    PointField.FLOAT64: ("d", 8),
}


def _unpack_cloud(msg: PointCloud2, wanted: tuple[str, ...]) -> list[dict[str, float]]:
    field_map = {f.name: f for f in msg.fields}
    specs: list[tuple[str, int, str]] = []
    for name in wanted:
        f = field_map.get(name)
        if f is None:
            continue
        fmt, _size = _DATATYPE.get(f.datatype, ("x", 0))
        if fmt == "x":
            continue
        specs.append((name, int(f.offset), fmt))
    if not specs:
        return []
    endian = "<" if not msg.is_bigendian else ">"
    points: list[dict[str, float]] = []
    step = msg.point_step
    data = bytes(msg.data)
    n = min(msg.width * msg.height, len(data) // max(step, 1))
    stride = max(1, n // 8000)  # keep summaries cheap enough for a 2 Hz loop
    for i in range(0, n, stride):
        off = i * step
        row = data[off : off + step]
        if len(row) < step:
            break
        rec: dict[str, float] = {}
        ok = True
        for name, offset, fmt in specs:
            try:
                rec[name] = float(struct.unpack_from(endian + fmt, row, offset)[0])
            except struct.error:
                ok = False
                break
        if ok:
            points.append(rec)
    return points


_UTM_ORIGIN_X = float(os.environ.get("AGENT_UTM_ORIGIN_X", "458054.71"))
_UTM_ORIGIN_Y = float(os.environ.get("AGENT_UTM_ORIGIN_Y", "5429310.68"))
try:
    SEM_LIDAR_FOV_DEG = float(os.environ.get("AGENT_SEM_LIDAR_FOV_DEG", "360"))
except ValueError:
    SEM_LIDAR_FOV_DEG = 360.0
SEM_LIDAR_FOV_DEG = min(360.0, max(1.0, SEM_LIDAR_FOV_DEG))

CAMERA_RGB_TOPIC = os.environ.get("AGENT_CAMERA_RGB", "/q1_camera").strip() or "/q1_camera"
CAMERA_SEM_COLOR_TOPIC = os.environ.get(
    "AGENT_CAMERA_SEMANTIC_COLOR", "/q1_camera_semantic/image_color"
).strip() or "/q1_camera_semantic/image_color"
CAMERA_RGB_JPEG = Path(
    os.environ.get("AGENT_CAMERA_RGB_JPEG", "/tmp/q1_camera_rgb.jpg")
).expanduser()
CAMERA_SEM_JPEG = Path(
    os.environ.get("AGENT_CAMERA_SEM_JPEG", "/tmp/q1_camera_semantic.jpg")
).expanduser()
CAMERA_MAX_SIDE = int(os.environ.get("AGENT_CAMERA_MAX_SIDE", "384"))


def _png_rgb(rgb: bytes, width: int, height: int) -> bytes:
    """Minimal RGB PNG (no extra deps)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + rgb[i * width * 3 : (i + 1) * width * 3] for i in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _resize_rgb(rgb: bytes, width: int, height: int, max_side: int) -> tuple[bytes, int, int]:
    if max(width, height) <= max_side or width < 2 or height < 2:
        return rgb, width, height
    if width >= height:
        nw, nh = max_side, max(1, int(round(height * max_side / width)))
    else:
        nh, nw = max_side, max(1, int(round(width * max_side / height)))
    out = bytearray(nw * nh * 3)
    for y in range(nh):
        sy = min(height - 1, int(y * height / nh))
        src = sy * width * 3
        dst = y * nw * 3
        for x in range(nw):
            sx = min(width - 1, int(x * width / nw))
            i = src + sx * 3
            j = dst + x * 3
            out[j : j + 3] = rgb[i : i + 3]
    return bytes(out), nw, nh


def _image_msg_to_rgb(msg: Image) -> tuple[bytes, int, int] | None:
    h, w, step = msg.height, msg.width, msg.step
    enc = (msg.encoding or "").lower()
    data = bytes(msg.data)
    if w < 1 or h < 1:
        return None
    if enc in ("rgb8", "8uc3") or (not enc and step == w * 3):
        bpp, bgr, alpha = 3, False, False
    elif enc in ("bgr8",):
        bpp, bgr, alpha = 3, True, False
    elif enc in ("rgba8",):
        bpp, bgr, alpha = 4, False, True
    elif enc in ("bgra8",):
        bpp, bgr, alpha = 4, True, True
    elif enc in ("mono8", "8uc1") or (not enc and step == w):
        out = bytearray(w * h * 3)
        for row in range(h):
            off = row * step
            dst = row * w * 3
            for col in range(w):
                v = data[off + col] if off + col < len(data) else 0
                i = dst + col * 3
                out[i] = out[i + 1] = out[i + 2] = v
        return bytes(out), w, h
    else:
        bpp, bgr, alpha = 3, False, False
        if step >= w * 4:
            bpp, alpha = 4, True
    out = bytearray(w * h * 3)
    for row in range(h):
        off = row * step
        dst = row * w * 3
        for col in range(w):
            i = off + col * bpp
            if i + 2 >= len(data):
                break
            c0, c1, c2 = data[i], data[i + 1], data[i + 2]
            r, g, b = (c2, c1, c0) if bgr else (c0, c1, c2)
            j = dst + col * 3
            out[j], out[j + 1], out[j + 2] = r, g, b
    return bytes(out), w, h


def _encode_still(rgb: bytes, width: int, height: int) -> tuple[bytes, str]:
    rgb, width, height = _resize_rgb(rgb, width, height, CAMERA_MAX_SIDE)
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore

        arr = np.frombuffer(rgb, dtype=np.uint8).reshape(height, width, 3)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            return bytes(buf), "jpeg"
    except Exception:
        pass
    try:
        from PIL import Image as PILImage  # type: ignore
        import io

        img = PILImage.frombytes("RGB", (width, height), rgb)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue(), "jpeg"
    except Exception:
        pass
    return _png_rgb(rgb, width, height), "png"


def _write_still(path: Path, payload: bytes, fmt: str) -> Path:
    dest = path.with_suffix(".jpg" if fmt == "jpeg" else ".png")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(payload)
    tmp.replace(dest)
    other = path.with_suffix(".png" if dest.suffix.lower() in {".jpg", ".jpeg"} else ".jpg")
    if other != dest:
        try:
            other.unlink()
        except OSError:
            pass
    return dest


def _in_forward_fov(x: float, y: float, fov_deg: float) -> bool:
    """True if sensor-frame (x forward, y left) bearing is inside ±FOV/2.

    Velodyne semantic lidar is 360° by default; a tighter cone is only used
    when AGENT_SEM_LIDAR_FOV_DEG is set below 360.
    """
    if fov_deg >= 359.0:
        return True
    half = math.radians(fov_deg) / 2.0
    return abs(math.atan2(y, x)) <= half


def _map_frames() -> list[str]:
    frames: list[str] = []
    for raw in (
        os.environ.get("AGENT_MAP_FRAME", "").strip(),
        os.environ.get("AGENT_WORLD", "").strip(),
        "open",
        "map",
        "utm_32n",
        "utm",
    ):
        if raw and raw not in frames:
            frames.append(raw)
    return frames


def _map_tf(tf_buffer, frame: str) -> tuple[float, float, float, float] | None:
    """Return (tx, ty, cos_yaw, sin_yaw) for sensor→map metres, or None."""
    if tf_buffer is None or not frame:
        return None
    for target in _map_frames():
        try:
            tf = tf_buffer.lookup_transform(target, frame, rclpy.time.Time())
        except Exception:
            continue
        t = tf.transform.translation
        q = tf.transform.rotation
        yaw = math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))
        tx, ty = float(t.x), float(t.y)
        if target in {"utm_32n", "utm"}:
            tx -= _UTM_ORIGIN_X
            ty -= _UTM_ORIGIN_Y
        return tx, ty, math.cos(yaw), math.sin(yaw)
    return None


def _apply_xy(x: float, y: float, tf: tuple[float, float, float, float] | None) -> tuple[float, float, str]:
    if tf is None:
        return x, y, "sensor"
    tx, ty, c, s = tf
    return c * x - s * y + tx, s * x + c * y + ty, "map"


class Q1SensorSummarizer(Node):
    def __init__(self) -> None:
        super().__init__(
            "q1_sensor_summarizer",
            parameter_overrides=[
                Parameter("use_sim_time", Parameter.Type.BOOL, True),
            ],
        )
        self._tf = None
        if Buffer is not None and TransformListener is not None:
            self._tf = Buffer()
            self._tf_listener = TransformListener(self._tf, self)

        self._pub_lidar = self.create_publisher(String, "/q1_sensor_summary/lidar", 10)
        self._pub_sem = self.create_publisher(String, "/q1_sensor_summary/semantic_lidar", 10)
        self._pub_cam = self.create_publisher(String, "/q1_sensor_summary/camera", 10)
        self._last_lidar = {"success": False, "error": "waiting_for_lidar"}
        self._last_sem = {"success": False, "error": "waiting_for_semantic_lidar"}
        self._last_cam = {"success": False, "error": "waiting_for_camera"}
        self._sem_from_labeled_cloud = False
        self._next_lidar = 0.0
        self._next_sem = 0.0
        self._next_cam = 0.0
        self._next_rgb = 0.0
        self._next_sem_img = 0.0

        qos = qos_profile_sensor_data
        cam_qos = QoSProfile(
            depth=5,
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
        )
        self.create_subscription(PointCloud2, "/q1_velodyne/points", self._on_lidar, qos)
        self.create_subscription(
            PointCloud2, "/q1_velodyne_semantic/points", self._on_semantic, qos
        )
        self.create_subscription(
            PointCloud2,
            "/q1_velodyne/points",
            self._on_semantic_intensity,
            qos,
        )
        # labels_map: gazebo bridge is RELIABLE. RGB / semantic color: typically
        # BEST_EFFORT (sensor_data); a RELIABLE-only sub never sees those frames.
        self.create_subscription(
            Image, "/q1_camera/labels_map", self._on_labels, cam_qos
        )
        for topic, cb in (
            (CAMERA_RGB_TOPIC, self._on_rgb),
            (CAMERA_SEM_COLOR_TOPIC, self._on_sem_color),
        ):
            self.create_subscription(Image, topic, cb, qos)
            self.create_subscription(Image, topic, cb, cam_qos)
        for still in (CAMERA_RGB_JPEG, CAMERA_SEM_JPEG):
            for suffix in (".jpg", ".jpeg", ".png"):
                try:
                    still.with_suffix(suffix).unlink()
                except OSError:
                    pass
        self.create_timer(0.5, self._republish)
        self.get_logger().info(
            f"Q1 sensor summarizer running (semantic lidar FOV {SEM_LIDAR_FOV_DEG:.0f} deg; "
            f"rgb={CAMERA_RGB_TOPIC} semantic_color={CAMERA_SEM_COLOR_TOPIC})"
        )

    def _due(self, attr: str, period: float = 0.5) -> bool:
        now = time.monotonic()
        if now < getattr(self, attr):
            return False
        setattr(self, attr, now + period)
        return True

    def _republish(self) -> None:
        self._publish(self._pub_lidar, self._last_lidar)
        self._publish(self._pub_sem, self._last_sem)
        self._publish(self._pub_cam, self._last_cam)

    def _publish(self, pub, payload: dict) -> None:
        msg = String()
        msg.data = json.dumps(payload)
        pub.publish(msg)

    def _on_lidar(self, msg: PointCloud2) -> None:
        if not self._due("_next_lidar"):
            return
        pts = _unpack_cloud(msg, ("x", "y", "z"))
        sectors = {"front": math.inf, "left": math.inf, "right": math.inf, "rear": math.inf}
        closest = math.inf
        closest_bearing = 0.0
        count = 0
        for p in pts:
            x, y, z = p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0)
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            if abs(z) > 3.0:
                continue
            rng = math.hypot(x, y)
            if rng < 0.3 or rng > 40.0:
                continue
            count += 1
            bearing = math.degrees(math.atan2(y, x))
            if rng < closest:
                closest = rng
                closest_bearing = bearing
            abs_b = abs(bearing)
            if abs_b <= 45:
                sectors["front"] = min(sectors["front"], rng)
            elif 45 < bearing <= 135:
                sectors["left"] = min(sectors["left"], rng)
            elif -135 <= bearing < -45:
                sectors["right"] = min(sectors["right"], rng)
            else:
                sectors["rear"] = min(sectors["rear"], rng)
        def _fin(v: float) -> float | None:
            return None if not math.isfinite(v) else round(v, 2)

        payload = {
            "success": True,
            "frame": msg.header.frame_id,
            "n_points": count,
            "min_range_m": _fin(closest),
            "min_range_bearing_deg": round(closest_bearing, 1) if math.isfinite(closest) else None,
            "sectors_min_m": {k: _fin(v) for k, v in sectors.items()},
        }
        self._last_lidar = payload
        self._publish(self._pub_lidar, payload)

    def _on_semantic_intensity(self, msg: PointCloud2) -> None:
        if self._sem_from_labeled_cloud:
            return
        self._on_semantic(msg, intensity_as_label=True)

    def _on_semantic(self, msg: PointCloud2, *, intensity_as_label: bool = False) -> None:
        if not self._due("_next_sem"):
            return
        pts = _unpack_cloud(msg, ("x", "y", "z", "class_id", "instance_id", "intensity"))
        tf = _map_tf(self._tf, msg.header.frame_id)
        fov_deg = SEM_LIDAR_FOV_DEG
        bins: dict[tuple[int, int, int], list[tuple[float, float]]] = defaultdict(list)
        for p in pts:
            cid = int(p.get("class_id", 0))
            if cid == 0 or intensity_as_label:
                cid = int(round(p.get("intensity", cid)))
            if cid in (0, 8, 23, 24, 31, 50, 51, 53):  # skip ground/sky/ego
                continue
            x, y = p.get("x", 0.0), p.get("y", 0.0)
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            if not _in_forward_fov(x, y, fov_deg):
                continue
            mx, my, _frame = _apply_xy(x, y, tf)
            gx, gy = int(math.floor(mx / 0.6)), int(math.floor(my / 0.6))
            bins[(cid, gx, gy)].append((mx, my))

        objects: list[dict] = []
        for (cid, _gx, _gy), xy in bins.items():
            if len(xy) < 3:
                continue
            cx = sum(p[0] for p in xy) / len(xy)
            cy = sum(p[1] for p in xy) / len(xy)
            objects.append(
                {
                    "class_id": cid,
                    "class": class_name(cid),
                    "x": round(cx, 2),
                    "y": round(cy, 2),
                    "n_points": len(xy),
                    "frame": "map" if tf is not None else "sensor",
                }
            )
        objects.sort(key=lambda o: -o["n_points"])
        merged: list[dict] = []
        for obj in objects:
            placed = False
            for other in merged:
                if other["class_id"] != obj["class_id"]:
                    continue
                if math.hypot(other["x"] - obj["x"], other["y"] - obj["y"]) > 2.0:
                    continue
                n = other["n_points"] + obj["n_points"]
                other["x"] = round(
                    (other["x"] * other["n_points"] + obj["x"] * obj["n_points"]) / n, 2
                )
                other["y"] = round(
                    (other["y"] * other["n_points"] + obj["y"] * obj["n_points"]) / n, 2
                )
                other["n_points"] = n
                placed = True
                break
            if not placed:
                merged.append(obj)
        payload = {
            "success": True,
            "frame": "map" if tf is not None else (msg.header.frame_id or "sensor"),
            "source": "intensity" if intensity_as_label else "semantic_cloud",
            "fov_deg": round(fov_deg, 1),
            "objects": merged[:12],
            "hint": (
                "Semantic lidar is 360°. Listed x/y are map-frame centroids. "
                "Empty objects means nothing labeled in range — do not invent "
                "coordinates."
                if fov_deg >= 359.0
                else (
                    f"Only objects inside a {fov_deg:.0f} deg forward cone are listed. "
                    "Empty objects means nothing in that cone. Do not invent coordinates."
                )
            ),
        }
        self._last_sem = payload
        if not intensity_as_label:
            self._sem_from_labeled_cloud = True
        self._publish(self._pub_sem, payload)

    def _on_labels(self, msg: Image) -> None:
        if not self._due("_next_cam"):
            return
        h, w, step = msg.height, msg.width, msg.step
        enc = (msg.encoding or "").lower()
        data = bytes(msg.data)
        labels: list[int] = []
        if enc in ("mono8", "8uc1") or (not enc and step == w):
            for row in range(h):
                off = row * step
                labels.extend(data[off : off + w])
        elif enc in ("mono16", "16uc1") or step == w * 2:
            for row in range(h):
                off = row * step
                rowb = data[off : off + w * 2]
                labels.extend(struct.unpack("<" + "H" * w, rowb) if len(rowb) == w * 2 else [])
        elif enc in ("rgb8", "bgr8", "rgba8", "bgra8") or step >= w * 3:
            bpp = 4 if "a" in enc or step >= w * 4 else 3
            bgr = enc.startswith("bgr")
            stride_y = max(1, h // 160)
            stride_x = max(1, w // 160)
            for row in range(0, h, stride_y):
                off = row * step
                for col in range(0, w, stride_x):
                    i = off + col * bpp
                    if i + 2 >= len(data):
                        break
                    c0, c1, c2 = data[i], data[i + 1], data[i + 2]
                    r, g, b = (c2, c1, c0) if bgr else (c0, c1, c2)
                    labels.append(class_id_from_rgb(r, g, b))
        else:
            payload = {"success": False, "error": f"unsupported_encoding:{msg.encoding}"}
            self._last_cam = payload
            self._publish(self._pub_cam, payload)
            return
        counts = Counter(labels)
        total = max(sum(counts.values()), 1)
        rows = []
        for cid, n in counts.most_common(16):
            if cid == 0 or n < 3:
                continue
            rows.append(
                {
                    "class_id": int(cid),
                    "class": class_name(int(cid)),
                    "pixels": int(n),
                    "pct": round(100.0 * n / total, 2),
                }
            )
        payload = {
            "success": True,
            "width": w,
            "height": h,
            "encoding": msg.encoding,
            "source": "labels_map",
            "n_sampled": int(total),
            "classes": rows,
        }
        self._last_cam = payload
        self._publish(self._pub_cam, payload)

    def _save_camera_still(self, msg: Image, dest: Path, due_attr: str) -> None:
        if not self._due(due_attr, 0.7):
            return
        parsed = _image_msg_to_rgb(msg)
        if parsed is None:
            return
        rgb, w, h = parsed
        payload, fmt = _encode_still(rgb, w, h)
        written = _write_still(dest, payload, fmt)
        flag = f"_logged_{due_attr}"
        if not getattr(self, flag, False):
            setattr(self, flag, True)
            self.get_logger().info(f"wrote {written} ({fmt} {w}x{h})")

    def _on_rgb(self, msg: Image) -> None:
        self._save_camera_still(msg, CAMERA_RGB_JPEG, "_next_rgb")

    def _on_sem_color(self, msg: Image) -> None:
        self._save_camera_still(msg, CAMERA_SEM_JPEG, "_next_sem_img")


def main() -> None:
    rclpy.init()
    node = Q1SensorSummarizer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()

from pathlib import Path

import cv2
import numpy as np

from tgexporter.image_filter import read_image_size
from tgexporter.video_cover import create_video_cover, select_key_frame


def test_create_video_cover_preserves_landscape_orientation(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    write_test_video(video, width=320, height=180)
    cover = tmp_path / "cover.jpg"

    assert create_video_cover(video, cover, "测试视频主题")
    assert cover.exists()
    assert read_image_size(cover) == (320, 180)


def test_select_key_frame_returns_frame(tmp_path: Path):
    video = tmp_path / "sample.mp4"
    write_test_video(video, width=180, height=320)

    frame = select_key_frame(video)

    assert frame is not None
    assert frame.shape[0] == 320
    assert frame.shape[1] == 180


def write_test_video(path: Path, width: int, height: int) -> None:
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, 8.0, (width, height))
    try:
        for index in range(12):
            frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame[:, :, 0] = 30 + index * 10
            frame[:, :, 1] = np.linspace(0, 255, width, dtype=np.uint8)
            frame[:, :, 2] = np.linspace(255, 0, height, dtype=np.uint8)[:, None]
            cv2.rectangle(frame, (10 + index * 3, 10), (width - 10, height - 10), (0, 255, 255), 3)
            writer.write(frame)
    finally:
        writer.release()


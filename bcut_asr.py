"""
必剪 (BCut) 语音识别接口 — 精简版

基于 B 站必剪的免费在线语音转文字 API，
将音频文件上传后获得文字转写结果。

参考: astrbot_plugin_biliVideo
"""

import json
import time
import logging
from typing import Optional, List

import requests

logger = logging.getLogger(__name__)

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"
API_CREATE_TASK = API_BASE_URL + "/task"
API_QUERY_RESULT = API_BASE_URL + "/task/result"

# 单次 HTTP 请求超时，以及整次 ASR 流程的总时限。
# 总时限必须明显小于“无限等待”；公开群机器人尤其需要这个保护。
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_TOTAL_TIMEOUT = 90.0
DEFAULT_POLL_INTERVAL = 1.0


class BcutASR:
    """必剪语音识别：上传音频 → 转写 → 返回纯文本"""

    HEADERS = {
        "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
        "Content-Type": "application/json",
    }

    def __init__(
        self,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ):
        if total_timeout <= 0:
            raise ValueError("total_timeout 必须大于 0")
        if connect_timeout <= 0 or read_timeout <= 0:
            raise ValueError("HTTP timeout 必须大于 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")

        self.session = requests.Session()
        self.total_timeout = float(total_timeout)
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.poll_interval = float(poll_interval)
        self._deadline: Optional[float] = None

        self.task_id: Optional[str] = None
        self._etags: List[str] = []
        self._in_boss_key: Optional[str] = None
        self._resource_id: Optional[str] = None
        self._upload_id: Optional[str] = None
        self._upload_urls: List[str] = []
        self._per_size: Optional[int] = None
        self._clips: Optional[int] = None
        self._download_url: Optional[str] = None

    def _remaining_time(self) -> float:
        """返回本次转写剩余时间；超过总时限时立即中止。"""
        if self._deadline is None:
            return self.total_timeout

        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"必剪 ASR 总处理时间超过 {self.total_timeout:.0f} 秒"
            )
        return remaining

    def _request_timeout(self):
        """根据总 deadline 动态收紧 requests 的 connect/read timeout。"""
        remaining = self._remaining_time()
        connect = min(self.connect_timeout, max(0.1, remaining))
        read = min(self.read_timeout, max(0.1, remaining))
        return (connect, read)

    # ── 上传流程 ──

    def _load_file(self, file_path: str) -> bytes:
        with open(file_path, "rb") as f:
            return f.read()

    def _upload(self, file_path: str) -> None:
        """申请上传并执行分片上传"""
        file_binary = self._load_file(file_path)
        if not file_binary:
            raise ValueError("无法读取文件数据")

        payload = json.dumps(
            {
                "type": 2,
                "name": "audio.mp3",
                "size": len(file_binary),
                "ResourceFileType": "mp3",
                "model_id": "8",
            }
        )

        resp = self.session.post(
            API_REQ_UPLOAD,
            data=payload,
            headers=self.HEADERS,
            timeout=self._request_timeout(),
        )
        resp.raise_for_status()
        resp_data = resp.json()["data"]

        self._in_boss_key = resp_data["in_boss_key"]
        self._resource_id = resp_data["resource_id"]
        self._upload_id = resp_data["upload_id"]
        self._upload_urls = resp_data["upload_urls"]
        self._per_size = resp_data["per_size"]
        self._clips = len(resp_data["upload_urls"])

        logger.info(f"[BcutASR] 申请上传成功, {self._clips} 分片")
        self._upload_parts(file_binary)
        self._commit_upload()

    def _upload_parts(self, file_binary: bytes) -> None:
        """上传音频分片"""
        for clip in range(self._clips):
            start = clip * self._per_size
            end = min((clip + 1) * self._per_size, len(file_binary))
            resp = self.session.put(
                self._upload_urls[clip],
                data=file_binary[start:end],
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._request_timeout(),
            )
            resp.raise_for_status()
            etag = resp.headers.get("Etag", "").strip('"')
            self._etags.append(etag)

    def _commit_upload(self) -> None:
        """提交上传"""
        data = json.dumps(
            {
                "InBossKey": self._in_boss_key,
                "ResourceId": self._resource_id,
                "Etags": ",".join(self._etags),
                "UploadId": self._upload_id,
                "model_id": "8",
            }
        )
        resp = self.session.post(
            API_COMMIT_UPLOAD,
            data=data,
            headers=self.HEADERS,
            timeout=self._request_timeout(),
        )
        resp.raise_for_status()
        resp_json = resp.json()

        if resp_json.get("code") != 0:
            raise Exception(f"上传提交失败: {resp_json.get('message', '未知错误')}")

        self._download_url = resp_json["data"]["download_url"]

    # ── 转写流程 ──

    def _create_task(self) -> str:
        """创建转写任务"""
        resp = self.session.post(
            API_CREATE_TASK,
            json={"resource": self._download_url, "model_id": "8"},
            headers=self.HEADERS,
            timeout=self._request_timeout(),
        )
        resp.raise_for_status()
        resp_json = resp.json()

        if resp_json.get("code") != 0:
            raise Exception(f"创建任务失败: {resp_json.get('message', '未知错误')}")

        self.task_id = resp_json["data"]["task_id"]
        return self.task_id

    def _query_result(self) -> dict:
        """查询转写结果"""
        resp = self.session.get(
            API_QUERY_RESULT,
            params={"model_id": 7, "task_id": self.task_id},
            headers=self.HEADERS,
            timeout=self._request_timeout(),
        )
        resp.raise_for_status()
        resp_json = resp.json()

        if resp_json.get("code") != 0:
            raise Exception(f"查询结果失败: {resp_json.get('message', '未知错误')}")

        return resp_json["data"]

    # ── 公开接口 ──

    def transcribe(self, file_path: str) -> str:
        """
        执行语音转写，返回完整文本字符串。

        :param file_path: 本地音频文件路径 (mp3)
        :return: 转写后的文本
        :raises Exception: 转写过程中的任何错误
        """
        try:
            self._deadline = time.monotonic() + self.total_timeout
            logger.info(
                f"[BcutASR] 开始处理文件: {file_path}，"
                f"总超时 {self.total_timeout:.0f} 秒"
            )

            # 重置状态
            self._etags = []
            self.task_id = None

            # 上传 → 创建任务
            self._upload(file_path)
            self._create_task()

            # 轮询等待结果。不要使用固定 500 次重试：
            # 用总 deadline 控制，避免 Tool 超时后后台线程继续跑很久。
            task_resp = None
            attempt = 0
            while True:
                task_resp = self._query_result()
                attempt += 1

                state = task_resp.get("state")
                if state == 4:  # 完成
                    break
                if state == 3:  # 失败
                    raise Exception(f"转写任务失败，状态码: {state}")

                remaining = self._remaining_time()
                if attempt == 1 or attempt % 10 == 0:
                    logger.info(
                        f"[BcutASR] 转录进行中... "
                        f"第 {attempt} 次查询，剩余约 {remaining:.0f} 秒"
                    )

                time.sleep(min(self.poll_interval, remaining))

            # 解析结果
            result_json = json.loads(task_resp["result"])
            full_text = ""
            for u in result_json.get("utterances", []):
                text = u.get("transcript", "").strip()
                full_text += text + " "

            return full_text.strip()

        except requests.Timeout as e:
            logger.error(f"[BcutASR] HTTP 请求超时: {e}")
            raise TimeoutError("必剪 ASR 网络请求超时") from e
        except TimeoutError as e:
            logger.error(f"[BcutASR] 转写超时: {e}")
            raise
        except Exception as e:
            logger.error(f"[BcutASR] 处理失败: {str(e)}")
            raise
        finally:
            self.session.close()
            self._deadline = None

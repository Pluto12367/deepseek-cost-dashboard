"""读取并保存 CI 失败发送端需要的运行输入。"""

import hashlib
import hmac
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
from uuid import UUID, uuid4

# 集中保存外部环境变量名称，避免加载函数中散落容易拼错的字符串。
INGEST_URL_ENV_NAME = "CFI_INGEST_URL"
INGEST_HMAC_SECRET_ENV_NAME = "CFI_INGEST_HMAC_SECRET"
JUNIT_XML_PATH_ENV_NAME = "CFI_JUNIT_XML_PATH"

GITHUB_REPOSITORY_ID_ENV_NAME = "CFI_GITHUB_REPOSITORY_ID"
GITHUB_REPOSITORY_ENV_NAME = "CFI_GITHUB_REPOSITORY"
GITHUB_RUN_ID_ENV_NAME = "CFI_GITHUB_RUN_ID"
GITHUB_RUN_ATTEMPT_ENV_NAME = "CFI_GITHUB_RUN_ATTEMPT"
GITHUB_WORKFLOW_NAME_ENV_NAME = "CFI_GITHUB_WORKFLOW_NAME"
GITHUB_JOB_KEY_ENV_NAME = "CFI_GITHUB_JOB_KEY"
GITHUB_JOB_NAME_ENV_NAME = "CFI_GITHUB_JOB_NAME"
GITHUB_COMMIT_SHA_ENV_NAME = "CFI_GITHUB_COMMIT_SHA"
GITHUB_HEAD_BRANCH_ENV_NAME = "CFI_GITHUB_HEAD_BRANCH"
# 三个请求头名称必须与服务端CI接入安全中间件保持一致。
INGEST_TIMESTAMP_HEADER_NAME = "X-CFI-Timestamp"
INGEST_NONCE_HEADER_NAME = "X-CFI-Nonce"
INGEST_SIGNATURE_HEADER_NAME = "X-CFI-Signature-256"

# 环境变量只有完整匹配该格式时，才能被转换为正整数。
POSITIVE_INTEGER_PATTERN = re.compile(r"[1-9][0-9]*")

# 仓库全名必须由 owner 和 repository 两部分组成。
REPOSITORY_FULL_NAME_PATTERN = re.compile(r"[^/\s]+/[^/\s]+")

# 完整 Git commit SHA 必须是40位十六进制字符。
COMMIT_SHA_PATTERN = re.compile(r"[0-9a-fA-F]{40}")

# 与服务端保持一致：HMAC共享密钥经过UTF-8编码后至少为32字节。
MINIMUM_HMAC_SECRET_BYTES = 32

# 与服务端请求模型保持一致，JUnit XML最多包含200万个字符。
MAX_JUNIT_XML_CHARACTERS = 2_000_000

# 与服务端当前默认配置一致，完整JSON请求体最大允许8MiB。
MAX_INGEST_BODY_BYTES = 8 * 1024 * 1024

# 单次请求最多等待10秒，避免GitHub Actions无限阻塞。
HTTP_REQUEST_TIMEOUT_SECONDS = 10

# 只有数据库事务已经提交或确认相同事件存在时，服务端才返回202。
ACCEPTED_HTTP_STATUS_CODE = 202

# 成功响应只包含少量状态字段，最多读取64KiB以限制异常响应占用。
MAX_RESPONSE_BODY_BYTES = 64 * 1024

# 第一次失败后等待2秒，第二次失败后等待5秒；第三次失败后不再等待。
RETRY_DELAYS_SECONDS = (2.0, 5.0)

# 这些状态通常表示服务端临时不可用、网关故障或请求受到限流。
RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

# 本机回环地址允许使用 HTTP；公网域名或公网 IP 都必须使用 HTTPS。
# 不可变集合：frozenset
LOCAL_HTTP_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
    }
)


class SenderConfigurationError(ValueError):
    """表示发送脚本缺少必要配置，或者配置值不符合协议要求。"""


class SenderReportError(ValueError):
    """表示JUnit失败报告无法作为有效输入读取。"""


class SenderPayloadError(ValueError):
    """表示发送端无法构造符合传输约束的请求Body。"""


class SenderResponseError(ValueError):
    """表示服务端响应不符合当前发送端认可的成功契约。"""


class SenderNetworkError(RuntimeError):
    """表示单次HTTP尝试因网络或超时问题未能取得响应。"""


class SenderHttpStatusError(RuntimeError):
    """表示服务端返回了当前成功契约不接受的HTTP状态。"""

    def __init__(self, status_code: int) -> None:
        """保存HTTP状态码，供后续重试策略判断。

        Args:
            status_code: 服务端返回的HTTP状态码。
        """

        # 后续重试逻辑需要根据状态码判断是否允许再次尝试。
        self.status_code = status_code

        # 调用父类构造方法，为异常保存正常的错误消息
        super().__init__(f"服务端返回HTTP {status_code}")


class _RejectRedirectHandler(HTTPRedirectHandler):
    """禁止发送端把已签名请求自动重定向到其他地址。"""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        """拒绝为3xx响应创建新的重定向请求。

        Returns:
            None: 通知urllib不创建重定向请求。
        """

        return


# CLI按顺序发送请求，共享一个禁止自动重定向的opener即可。
HTTP_OPENER = build_opener(_RejectRedirectHandler())


@dataclass(frozen=True)
class SenderConfig:
    """保存已经完成类型转换和格式校验的发送端输入。

    Attributes:
        ingest_url: CI Failure Intelligence 的完整接入 URL。
        hmac_secret: 经过 UTF-8 编码的 HMAC 共享密钥。
        junit_xml_path: pytest 生成的 JUnit XML 文件路径。
        github_repository_id: GitHub 仓库的数字身份。
        github_repository: `owner/repository` 格式的仓库全名。
        github_run_id: 当前 GitHub Actions workflow run 的数字身份。
        github_run_attempt: 当前 workflow run 的执行次数。
        github_workflow_name: GitHub Actions workflow 的显示名称。
        github_job_key: 当前 Job 的稳定机器标识。
        github_job_name: 当前 Job 的可读名称。
        github_commit_sha: 本次实际签出并测试的提交 SHA。
        github_head_branch: 产生本次失败的来源分支。
    """

    ingest_url: str

    # 密钥仍保存在对象中，但不会出现在 dataclass 自动生成的打印结果里。
    hmac_secret: bytes = field(repr=False)

    junit_xml_path: Path

    github_repository_id: int
    github_repository: str

    github_run_id: int
    github_run_attempt: int
    github_workflow_name: str
    github_job_key: str
    github_job_name: str
    github_commit_sha: str
    github_head_branch: str


def _require_environment_value(
    environ: Mapping[str, str],
    name: str,
) -> str:
    """读取一个必须存在且不能是空字符串的环境变量。

    Args:
        environ: 尚未完成业务校验的环境变量映射。
        name: 需要读取的环境变量名称。

    Returns:
        环境变量中保存的原始字符串。

    Raises:
        SenderConfigurationError: 环境变量缺失或者值为空字符串。
    """

    # 使用 get() 区分“没有配置”和正常存在的字符串值。
    value = environ.get(name)

    if value is None or value == "":
        raise SenderConfigurationError(f"{name} 未配置或为空")

    return value


def _parse_positive_integer(
    raw_value: str,
    environment_name: str,
) -> int:
    """将严格的十进制正整数字符串转换为整数。

    Args:
        raw_value: 从环境变量读取的原始字符串。
        environment_name: 该值所属的环境变量名称，用于生成错误信息。

    Returns:
        完成格式校验后的正整数。

    Raises:
        SenderConfigurationError: 字符串不是无前导零的十进制正整数。
    """

    # fullmatch() 要求整个字符串符合规则，不能混入空格、符号或字母。
    if POSITIVE_INTEGER_PATTERN.fullmatch(raw_value) is None:
        raise SenderConfigurationError(
            f"{environment_name} 必须是无前导零的十进制正整数"
        )

    return int(raw_value)


def _validate_bounded_text(
    raw_value: str,
    environment_name: str,
    *,
    max_length: int = 255,
) -> str:
    """检查普通文本环境变量是否有实际内容且未超过长度限制。

    Args:
        raw_value: 从环境变量读取的原始字符串。
        environment_name: 该值所属的环境变量名称，用于生成错误信息。
        max_length: 允许的最大字符数量。

    Returns:
        校验通过后的原始字符串。

    Raises:
        SenderConfigurationError: 字符串仅包含空白字符或超过长度限制。
    """

    # 不直接 strip 后返回，避免悄悄修改 GitHub 提供的原始运行信息。
    if not raw_value.strip():
        raise SenderConfigurationError(f"{environment_name} 不能只包含空白字符")

    if len(raw_value) > max_length:
        raise SenderConfigurationError(
            f"{environment_name} 不能超过 {max_length} 个字符"
        )

    return raw_value


def _validate_ingest_url(
    raw_value: str,
    environment_name: str,
) -> str:
    """检查 CI 失败接入地址是否符合本项目的传输安全规则。

    Args:
        raw_value: 从环境变量读取的完整接入 URL。
        environment_name: URL 所属的环境变量名称，用于生成错误信息。

    Returns:
        校验通过后的原始 URL。

    Raises:
        SenderConfigurationError: URL 无法解析，或者违反接入地址规则。
    """

    # URL 中不能混入未经编码的空格、换行符或其他空白字符。
    if any(character.isspace() for character in raw_value):
        raise SenderConfigurationError(f"{environment_name} 不能包含空白字符")

    try:
        parsed_url = urlsplit(raw_value)
        # 主动访问 port，让标准库检查端口格式和端口范围。
        _ = parsed_url.port
    except ValueError as e:
        raise SenderConfigurationError(f"{environment_name} 不是有效的接入 URL") from e
    scheme = parsed_url.scheme.lower()
    hostname = parsed_url.hostname

    if scheme not in ("http", "https"):
        raise SenderConfigurationError(f"{environment_name}只允许使用HTTP或HTTPS")

    if hostname is None:
        raise SenderConfigurationError(f"{environment_name} 缺少主机名")

    # 本项目通过 HMAC 请求头鉴权，不允许在 URL 中携带认证信息。
    if parsed_url.username is not None or parsed_url.password is not None:
        raise SenderConfigurationError(f"{environment_name} 不能包含用户名或密码")

    # 这里需要完整的 API 地址，不能只配置服务器根地址。
    if not parsed_url.path or parsed_url.path == "/":
        raise SenderConfigurationError(f"{environment_name} 必须包含接入接口路径")

    if "?" in raw_value or "#" in raw_value:
        raise SenderConfigurationError(f"{environment_name} 不能包含查询参数或片段")

    # 本机开发允许 HTTP；公网域名或公网 IP 必须使用 HTTPS。
    if scheme == "http" and hostname.lower() not in LOCAL_HTTP_HOSTS:
        raise SenderConfigurationError(
            f"{environment_name} 只有本机回环地址可以使用 HTTP，"
            "公网域名或公网 IP 必须使用 HTTPS"
        )

    return raw_value


def _validate_repository_full_name(
    raw_value: str,
    environment_name: str,
) -> str:
    """检查 GitHub 仓库全名是否为 `owner/repository` 格式。

    Args:
        raw_value: GitHub Actions 提供的仓库全名。
        environment_name: 仓库全名所属的环境变量名称。

    Returns:
        校验通过后的原始仓库全名。

    Raises:
        SenderConfigurationError: 仓库全名长度或格式不符合接口契约。
    """

    # 先复用普通文本校验，并与服务端的200字符上限保持一致。
    validated_value = _validate_bounded_text(
        raw_value,
        environment_name,
        max_length=200,
    )

    # fullmatch()要求整个值严格符合owner/repository结构。
    if REPOSITORY_FULL_NAME_PATTERN.fullmatch(validated_value) is None:
        raise SenderConfigurationError(
            f"{environment_name} 必须使用 owner/repository 格式"
        )

    return validated_value


def _validate_commit_sha(
    raw_value: str,
    environment_name: str,
) -> str:
    """检查提交身份是否为完整的40位 Git commit SHA。

    Args:
        raw_value: 本次实际签出并完成测试的commit SHA。
        environment_name: commit SHA所属的环境变量名称。

    Returns:
        校验通过后的原始commit SHA。

    Raises:
        SenderConfigurationError: commit SHA不是40位十六进制字符。
    """

    if COMMIT_SHA_PATTERN.fullmatch(raw_value) is None:
        raise SenderConfigurationError(
            f"{environment_name} 必须是40位十六进制commit SHA"
        )

    return raw_value


def _encode_hmac_secret(
    raw_value: str,
    environment_name: str,
) -> bytes:
    """将HMAC共享密钥编码为bytes并检查最小安全长度。

    Args:
        raw_value: 从环境变量读取的原始HMAC共享密钥。
        environment_name: 密钥所属的环境变量名称，用于生成安全的错误信息。

    Returns:
        经过UTF-8编码且长度满足要求的密钥字节。

    Raises:
        SenderConfigurationError: 密钥的UTF-8编码长度不足32字节。
    """

    # HMAC处理的是bytes，发送端和服务端必须采用相同的UTF-8编码。
    secret_bytes = raw_value.encode("utf-8")

    if len(secret_bytes) < MINIMUM_HMAC_SECRET_BYTES:
        # 错误信息只包含配置名称，绝不能包含真实密钥。
        raise SenderConfigurationError(
            f"{environment_name} 的UTF-8编码长度必须至少为"
            f"{MINIMUM_HMAC_SECRET_BYTES}字节"
        )

    return secret_bytes


def _validate_junit_xml_path(
    raw_value: str,
    environment_name: str,
) -> Path:
    """检查JUnit XML路径是否指向一个真实文件。

    Args:
        raw_value: 从环境变量读取的JUnit XML文件路径。
        environment_name: 路径所属的环境变量名称，用于生成错误信息。

    Returns:
        指向现有JUnit XML文件的Path对象。

    Raises:
        SenderConfigurationError: 路径不存在或者没有指向普通文件。
    """

    # 保留相对路径，使脚本可以在本机和GitHub Runner仓库目录中复用。
    junit_xml_path = Path(raw_value)

    if not junit_xml_path.is_file():
        raise SenderConfigurationError(
            f"{environment_name} 必须指向存在的JUnit XML文件"
        )

    return junit_xml_path


def load_sender_config(environ: Mapping[str, str]) -> SenderConfig:
    """从环境变量映射创建经过完整校验的发送端配置。

    Args:
        environ: GitHub Actions或当前进程提供的原始环境变量映射。
            所有键和值在进入本函数时仍然是字符串。

    Returns:
        所有必要字段均已读取、转换和校验的SenderConfig对象。

    Raises:
        SenderConfigurationError: 必要环境变量缺失，或者任意字段不符合
            发送协议、服务端数据契约或本地文件要求。
    """

    # 第一组：接入地址和共享密钥决定请求发往哪里、怎样生成签名。
    raw_ingest_url = _require_environment_value(
        environ,
        INGEST_URL_ENV_NAME,
    )
    ingest_url = _validate_ingest_url(
        raw_ingest_url,
        INGEST_URL_ENV_NAME,
    )

    raw_hmac_secret = _require_environment_value(
        environ,
        INGEST_HMAC_SECRET_ENV_NAME,
    )
    hmac_secret = _encode_hmac_secret(
        raw_hmac_secret,
        INGEST_HMAC_SECRET_ENV_NAME,
    )

    # 第二组：报告路径必须在发送前指向pytest实际生成的文件。
    raw_junit_xml_path = _require_environment_value(
        environ,
        JUNIT_XML_PATH_ENV_NAME,
    )
    junit_xml_path = _validate_junit_xml_path(
        raw_junit_xml_path,
        JUNIT_XML_PATH_ENV_NAME,
    )

    # 第三组：仓库身份在不同workflow run之间保持稳定。
    raw_repository_id = _require_environment_value(
        environ,
        GITHUB_REPOSITORY_ID_ENV_NAME,
    )
    github_repository_id = _parse_positive_integer(
        raw_repository_id,
        GITHUB_REPOSITORY_ID_ENV_NAME,
    )

    raw_repository = _require_environment_value(
        environ,
        GITHUB_REPOSITORY_ENV_NAME,
    )
    github_repository = _validate_repository_full_name(
        raw_repository,
        GITHUB_REPOSITORY_ENV_NAME,
    )

    # 第四组：运行身份描述本次实际执行的workflow和Job。
    raw_run_id = _require_environment_value(
        environ,
        GITHUB_RUN_ID_ENV_NAME,
    )
    github_run_id = _parse_positive_integer(
        raw_run_id,
        GITHUB_RUN_ID_ENV_NAME,
    )

    raw_run_attempt = _require_environment_value(
        environ,
        GITHUB_RUN_ATTEMPT_ENV_NAME,
    )
    github_run_attempt = _parse_positive_integer(
        raw_run_attempt,
        GITHUB_RUN_ATTEMPT_ENV_NAME,
    )

    raw_workflow_name = _require_environment_value(
        environ,
        GITHUB_WORKFLOW_NAME_ENV_NAME,
    )
    github_workflow_name = _validate_bounded_text(
        raw_workflow_name,
        GITHUB_WORKFLOW_NAME_ENV_NAME,
    )

    raw_job_key = _require_environment_value(
        environ,
        GITHUB_JOB_KEY_ENV_NAME,
    )
    github_job_key = _validate_bounded_text(
        raw_job_key,
        GITHUB_JOB_KEY_ENV_NAME,
    )

    raw_job_name = _require_environment_value(
        environ,
        GITHUB_JOB_NAME_ENV_NAME,
    )
    github_job_name = _validate_bounded_text(
        raw_job_name,
        GITHUB_JOB_NAME_ENV_NAME,
    )

    raw_commit_sha = _require_environment_value(
        environ,
        GITHUB_COMMIT_SHA_ENV_NAME,
    )
    github_commit_sha = _validate_commit_sha(
        raw_commit_sha,
        GITHUB_COMMIT_SHA_ENV_NAME,
    )

    raw_head_branch = _require_environment_value(
        environ,
        GITHUB_HEAD_BRANCH_ENV_NAME,
    )
    github_head_branch = _validate_bounded_text(
        raw_head_branch,
        GITHUB_HEAD_BRANCH_ENV_NAME,
    )

    # 只有前面所有读取和校验均成功，才创建最终配置对象。
    return SenderConfig(
        ingest_url=ingest_url,
        hmac_secret=hmac_secret,
        junit_xml_path=junit_xml_path,
        github_repository_id=github_repository_id,
        github_repository=github_repository,
        github_run_id=github_run_id,
        github_run_attempt=github_run_attempt,
        github_workflow_name=github_workflow_name,
        github_job_key=github_job_key,
        github_job_name=github_job_name,
        github_commit_sha=github_commit_sha,
        github_head_branch=github_head_branch,
    )


def read_junit_xml(junit_xml_path: Path) -> str:
    """按UTF-8读取一次受字符上限保护的JUnit XML。

    Args:
        junit_xml_path: 已由配置加载阶段确认存在的JUnit XML文件路径。

    Returns:
        非空且不超过服务端字符上限的JUnit XML文本。

    Raises:
        SenderReportError: 文件无法读取、不是有效UTF-8、内容为空，
            或者字符数量超过服务端限制。
    """

    try:
        with junit_xml_path.open(mode="r", encoding="utf-8") as junit_file:
            # 多读取一个字符，用来区分“恰好达到上限”和“已经超限”
            junit_xml = junit_file.read(MAX_JUNIT_XML_CHARACTERS + 1)

    # OSError覆盖： - 文件被删除； - 权限不足； - 文件系统读取失败。
    # UnicodeDecodeError表示文件字节不能按UTF-8解释。
    except (OSError, UnicodeDecodeError) as e:
        raise SenderReportError("JUnit XML无法按UTF-8读取") from e
    if not junit_xml:
        raise SenderReportError("JUnit XML不能为空")

    if len(junit_xml) > MAX_JUNIT_XML_CHARACTERS:
        raise SenderReportError(f"JUnit XML不能超过{MAX_JUNIT_XML_CHARACTERS}个字符")

    return junit_xml


def build_request_body(
    config: SenderConfig,
    junit_xml: str,
) -> bytes:
    """构造一次符合服务端数据契约的UTF-8 JSON请求体。

    Args:
        config: 已完成读取、类型转换和格式校验的发送端配置。
        junit_xml: 已完成UTF-8读取和字符上限检查的JUnit XML文本。

    Returns:
        可供HMAC签名并由HTTP客户端原样发送的JSON字节。

    Raises:
        SenderPayloadError: 完整JSON请求体超过服务端传输字节上限。
    """

    # 字段名称和嵌套结构必须与服务端CiFailureIngestRequest保持一致。
    payload = {
        "repository": {
            "github_repository_id": config.github_repository_id,
            "full_name": config.github_repository,
        },
        "run": {
            "run_id": config.github_run_id,
            "run_attempt": config.github_run_attempt,
            "workflow_name": config.github_workflow_name,
            "job_key": config.github_job_key,
            "job_name": config.github_job_name,
            "commit_sha": config.github_commit_sha,
            "head_branch": config.github_head_branch,
        },
        "junit_xml": junit_xml,
    }

    # 把准备好的 Python 业务数据，变成真正要通过 HTTP 发送的 JSON 字节。
    raw_body = json.dumps(
        payload,
        ensure_ascii=False,
        # 字段与字段之间用：, 键与值之间用：:
        separators=(",", ":"),
        # 按键名排序
        sort_keys=True,
    ).encode("utf-8")

    # JUnit字符数合规，不代表包含JSON结构后的UTF-8字节数一定合规。
    if len(raw_body) > MAX_INGEST_BODY_BYTES:
        raise SenderPayloadError(f"完整JSON请求体不能超过{MAX_INGEST_BODY_BYTES}字节")

    return raw_body


def create_security_headers(
    hmac_secret: bytes,
    raw_body: bytes,
    *,
    timestamp_seconds: int | None = None,
    nonce: UUID | None = None,
) -> dict[str, str]:
    """为一次HTTP尝试创建v1安全请求头。

    Args:
        hmac_secret: 与服务端共享的HMAC密钥字节。
        raw_body: 本次业务事件已经固定下来的原始JSON字节。
        timestamp_seconds: 测试时可指定的Unix秒；未提供时读取当前时间。
        nonce: 测试时可指定的UUID v4；未提供时创建新的随机UUID v4。

    Returns:
        包含timestamp、nonce和HMAC-SHA256签名的HTTP请求头。
    """

    # 正常发送读取当前Unix秒；测试可以传入固定秒数得到稳定结果。
    resolved_timestamp_seconds = (
        int(time.time()) if timestamp_seconds is None else timestamp_seconds
    )

    # 每次真实HTTP尝试都必须调用uuid4()生成新的发送号码。
    resolved_nonce = uuid4() if nonce is None else nonce

    # 服务端要求timestamp使用十进制ASCII文本。
    timestamp_text = str(resolved_timestamp_seconds)

    # UUID对象转换为标准8-4-4-4-12小写文本。
    nonce_text = str(resolved_nonce)

    # 签名输入必须严格遵守v1协议，最后拼接未经修改的原始Body。
    signing_message = (
        b"v1."
        + timestamp_text.encode("ascii")
        + b"."
        + nonce_text.encode("ascii")
        + b"."
        + raw_body
    )

    # hexdigest()返回服务端要求的64位小写十六进制SHA-256摘要。
    digest = hmac.new(
        hmac_secret,
        msg=signing_message,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # 签名值的v1=前缀用于标明当前采用的签名协议版本。
    return {
        INGEST_TIMESTAMP_HEADER_NAME: timestamp_text,
        INGEST_NONCE_HEADER_NAME: nonce_text,
        INGEST_SIGNATURE_HEADER_NAME: f"v1={digest}",
    }


def validate_success_response(
    response_body: bytes,
    config: SenderConfig,
) -> None:
    """核对服务端回执，确认响应符合当前 accepted 成功契约。

    Args:
        response_body: 服务端返回的原始响应Body字节。
        config: 本次发送使用的配置，用于核对运行身份。

    Returns:
        None: 响应结构、状态和运行身份全部正确。

    Raises:
        SenderResponseError: 响应不是合法JSON、结构错误、状态错误，
            或者回传的运行身份与本次请求不一致。
    """

    try:
        # json.loads()会将UTF-8 JSON字节转换成Python对象。
        response_data = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SenderResponseError("服务端成功响应不是合法的UTF-8 JSON") from error

    # 成功响应的JSON顶层必须是对象，不能是列表、字符串或数字。
    if not isinstance(response_data, dict):
        raise SenderResponseError("服务端成功响应必须是JSON对象")

    # 当前客户端只认可这四个字段，防止接口契约悄悄发生变化。
    expected_fields = {
        "status",
        "run_id",
        "run_attempt",
        "job_key",
    }

    # set(response_data):取得的是字典所有键。
    if set(response_data) != expected_fields:
        raise SenderResponseError("服务端成功响应字段不符合当前契约")

    # accepted表示原始CI事件已经提交，或相同事件已经可靠存在。
    if response_data["status"] != "accepted":
        raise SenderResponseError("服务端未确认原始CI失败事件已保存")

    # 使用type(...) is int，避免把JSON中的true误认为整数1。
    if type(response_data["run_id"]) is not int:
        raise SenderResponseError("服务端回传的run_id类型错误")

    if type(response_data["run_attempt"]) is not int:
        raise SenderResponseError("服务端回传的run_attempt类型错误")

    if type(response_data["job_key"]) is not str:
        raise SenderResponseError("服务端回传的job_key类型错误")

    # 响应身份必须与本次发送的CI事件完全一致。
    if (
        response_data["run_id"] != config.github_run_id
        or response_data["run_attempt"] != config.github_run_attempt
        or response_data["job_key"] != config.github_job_key
    ):
        raise SenderResponseError("服务端回传的运行身份与本次请求不一致")


def send_single_attempt(
    config: SenderConfig,
    raw_body: bytes,
) -> None:
    """执行一次带有v1安全信封的CI失败HTTP投递。

    Args:
        config: 已完成读取和校验的发送端配置。
        raw_body: 已固定并完成大小检查的原始JSON请求字节。

    Returns:
        None: 服务端返回HTTP 202，且成功响应契约校验通过。

    Raises:
        SenderNetworkError: DNS、连接、TLS或超时导致未取得响应。
        SenderHttpStatusError: 服务端返回的HTTP状态不是202。
        SenderResponseError: HTTP 202响应过大或不符合成功响应契约。
    """

    # 每次调用都重新生成timestamp、nonce和signature。
    security_headers = create_security_headers(
        config.hmac_secret,
        raw_body,
    )

    # HTTP客户端还需要知道Body是UTF-8 JSON，并声明期望JSON响应。
    request_headers = {
        **security_headers,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }

    # 直接发送已经参与签名的raw_body，不能在这里重新序列化JSON。
    request = Request(
        url=config.ingest_url,
        data=raw_body,
        headers=request_headers,
        method="POST",
    )

    try:
        # with代码块结束时会关闭HTTP响应及其底层网络连接。
        with HTTP_OPENER.open(
            request,
            timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            status_code = response.status

            # 多读取一个字节，用来区分“恰好达到上限”和“已经超限”。
            response_body = response.read(MAX_RESPONSE_BODY_BYTES + 1)

    # HTTPError必须写在URLError前，因为HTTPError是URLError的子类。
    except HTTPError as error:
        status_code = error.code

        # HTTPError同时持有响应流；无需读取错误Body时主动释放资源。
        error.close()

        raise SenderHttpStatusError(status_code) from error

    except (URLError, TimeoutError) as error:
        raise SenderNetworkError("CI接入请求因网络或超时问题失败") from error

    # urllib通常把非2xx转换成HTTPError；其他2xx也不能冒充当前成功契约。
    if status_code != ACCEPTED_HTTP_STATUS_CODE:
        raise SenderHttpStatusError(status_code)

    if len(response_body) > MAX_RESPONSE_BODY_BYTES:
        raise SenderResponseError("服务端成功响应超过允许的大小")

    # HTTP 202只是第一层条件，还必须核对accepted状态和CI运行身份。
    validate_success_response(
        response_body,
        config,
    )


def send_with_retries(
    config: SenderConfig,
    raw_body: bytes,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """按照固定退避策略执行最多三次CI失败投递。

    Args:
        config: 已完成读取和校验的发送端配置。
        raw_body: 所有尝试共同复用的原始JSON请求字节。
        sleep: 两次尝试之间使用的等待函数；测试可传入替代实现。

    Returns:
        None: 任意一次尝试通过完整成功响应校验。

    Raises:
        SenderNetworkError: 三次尝试均发生网络错误。
        SenderHttpStatusError: 收到不可重试状态，或可重试状态耗尽三次。
        SenderResponseError: HTTP 202响应不符合成功契约，不进行重试。
    """

    # 两个等待间隔对应三次发送：第一次立即执行，后面最多再执行两次。
    total_attempts = len(RETRY_DELAYS_SECONDS) + 1

    for attempt_index in range(total_attempts):
        try:
            # 单次发送函数会为每次尝试创建新的timestamp、nonce和signature。
            send_single_attempt(
                config,
                raw_body,
            )

            # 任意一次完整成功后立即结束，不再执行多余请求。
            return

        except SenderNetworkError:
            # 最后一次仍然失败时保留并继续抛出原始网络异常。
            if attempt_index == total_attempts - 1:
                raise

        except SenderHttpStatusError as error:
            # 永久性HTTP错误立即停止；可重试错误在第三次后同样停止。
            if (
                error.status_code not in RETRYABLE_HTTP_STATUS_CODES
                or attempt_index == total_attempts - 1
            ):
                raise

        # 只有前面的失败允许重试且尚有下一次机会时，才会执行等待。
        sleep(RETRY_DELAYS_SECONDS[attempt_index])


def main(
    environ: Mapping[str, str] | None = None,
) -> int:
    """执行一次完整的CI失败报告投递命令。

    Args:
        environ: 可选环境变量映射；未提供时读取当前进程环境变量。

    Returns:
        int: 原始CI失败事件确认保存时返回0；
            已知配置、报告、构造或投递失败时返回1。
    """

    # 正常命令行运行读取真实环境；测试可以传入独立字典避免污染进程环境。
    resolved_environ = os.environ if environ is None else environ

    try:
        # 先完成配置读取和类型转换，任何无效输入都会在网络请求前终止。
        config = load_sender_config(resolved_environ)

        # 报告只读取一次，后续所有网络尝试复用同一份业务内容。
        junit_xml = read_junit_xml(config.junit_xml_path)

        # 请求Body只序列化一次，确保每次重试签名和发送的字节完全相同。
        raw_body = build_request_body(
            config,
            junit_xml,
        )

        # 最多执行三次投递；单次尝试内部负责创建新的安全信封。
        send_with_retries(
            config,
            raw_body,
        )

    except SenderConfigurationError as error:
        print(f"CI失败投递配置错误：{error}", file=sys.stderr)
        return 1

    except SenderReportError as error:
        print(f"CI失败报告错误：{error}", file=sys.stderr)
        return 1

    except SenderPayloadError as error:
        print(f"CI失败请求构造错误：{error}", file=sys.stderr)
        return 1

    except (
        SenderNetworkError,
        SenderHttpStatusError,
        SenderResponseError,
    ) as error:
        print(f"CI失败投递失败：{error}", file=sys.stderr)
        return 1

    # 这里只确认原始事件已经持久化，不代表JUnit已经解析或诊断完成。
    print(
        "原始 CI 失败事件已保存："
        f"run_id={config.github_run_id} "
        f"run_attempt={config.github_run_attempt} "
        f"job_key={config.github_job_key}"
    )
    return 0


if __name__ == "__main__":
    # SystemExit把main()返回值交给PowerShell或GitHub Actions作为进程退出码。
    raise SystemExit(main())

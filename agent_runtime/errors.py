"""Standard error codes and response schema for Fusion Agent Studio API.

Aligned with PRD error specification: consistent error body across all endpoints.
Callers: api_server.py, auth_middleware.py, rate_limiter.py, daemon_server.py.
Data schema: ErrorResponse with code, type, message, user_message, param.
User instruction: "对比fusion-agent-studio看还有哪些缺失，尽快补齐"
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ErrorType(str, Enum):
    AUTH_ERROR = "auth_error"
    INVALID_REQUEST_ERROR = "invalid_request_error"
    RESOURCE_NOT_FOUND = "resource_not_found"
    PERMISSION_ERROR = "permission_error"
    RATE_LIMIT_ERROR = "rate_limit_error"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODEL_ERROR = "model_error"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    INTERNAL_SERVER_ERROR = "internal_server_error"


class ErrorCode(str, Enum):
    API_KEY_MISSING = "API_KEY_MISSING"
    API_KEY_INVALID = "API_KEY_INVALID"
    API_KEY_IP_FORBIDDEN = "API_KEY_IP_FORBIDDEN"
    API_KEY_AGENT_RESTRICT = "API_KEY_AGENT_RESTRICT"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    PARAM_REQUIRED = "PARAM_REQUIRED"
    PARAM_FORMAT_ERROR = "PARAM_FORMAT_ERROR"
    STREAM_CONFLICT = "STREAM_CONFLICT"
    MAX_TOKEN_OVER_LIMIT = "MAX_TOKEN_OVER_LIMIT"
    ATTACHMENT_INVALID = "ATTACHMENT_INVALID"
    MODEL_NOT_SUPPORT = "MODEL_NOT_SUPPORT"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
    CONNECTOR_NOT_FOUND = "CONNECTOR_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    KNOWLEDGE_BASE_NOT_FOUND = "KNOWLEDGE_BASE_NOT_FOUND"
    RESOURCE_PRIVATE = "RESOURCE_PRIVATE"
    AGENT_NOT_PUBLISHED = "AGENT_NOT_PUBLISHED"
    OPERATION_FORBIDDEN = "OPERATION_FORBIDDEN"
    RATE_LIMIT_REACHED = "RATE_LIMIT_REACHED"
    MONTHLY_QUOTA_EXHAUSTED = "MONTHLY_QUOTA_EXHAUSTED"
    DAILY_QUOTA_EXHAUSTED = "DAILY_QUOTA_EXHAUSTED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CONTENT_POLICY = "MODEL_CONTENT_POLICY"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    SEARCH_FAILED = "SEARCH_FAILED"
    DEEP_RESEARCH_FAIL = "DEEP_RESEARCH_FAIL"
    CONNECTOR_AUTH_EXPIRED = "CONNECTOR_AUTH_EXPIRED"
    FILE_PARSE_FAILED = "FILE_PARSE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    INJECTION_DETECTED = "INJECTION_DETECTED"


_ERROR_REGISTRY: dict[str, dict[str, str]] = {
    ErrorCode.API_KEY_MISSING: {
        "type": ErrorType.AUTH_ERROR,
        "message": "Request header missing x-api-key",
        "user_message": "身份凭证缺失，请填写 API 密钥",
    },
    ErrorCode.API_KEY_INVALID: {
        "type": ErrorType.AUTH_ERROR,
        "message": "API Key does not exist or has been revoked",
        "user_message": "API 密钥无效，请核对或重新创建密钥",
    },
    ErrorCode.API_KEY_IP_FORBIDDEN: {
        "type": ErrorType.AUTH_ERROR,
        "message": "Current IP not in key IP whitelist",
        "user_message": "访问 IP 受限，请联系管理员配置 IP 白名单",
    },
    ErrorCode.API_KEY_AGENT_RESTRICT: {
        "type": ErrorType.AUTH_ERROR,
        "message": "This key is restricted from calling the requested Agent",
        "user_message": "当前密钥无权限调用此智能体",
    },
    ErrorCode.TOKEN_EXPIRED: {
        "type": ErrorType.AUTH_ERROR,
        "message": "Login session has expired",
        "user_message": "登录超时，请重新登录控制台",
    },
    ErrorCode.PARAM_REQUIRED: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "Required parameter is missing",
        "user_message": "参数不能为空",
    },
    ErrorCode.PARAM_FORMAT_ERROR: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "Parameter format is invalid",
        "user_message": "参数格式错误，请检查输入内容",
    },
    ErrorCode.STREAM_CONFLICT: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "stream parameter must be true or false",
        "user_message": "stream 参数仅支持 true/false",
    },
    ErrorCode.MAX_TOKEN_OVER_LIMIT: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "Requested max_tokens exceeds platform limit",
        "user_message": "最大输出 Token 数值超出上限",
    },
    ErrorCode.ATTACHMENT_INVALID: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "Attachment file ID does not exist",
        "user_message": "上传文件失效，请重新上传",
    },
    ErrorCode.MODEL_NOT_SUPPORT: {
        "type": ErrorType.INVALID_REQUEST_ERROR,
        "message": "Selected model version is unavailable",
        "user_message": "当前所选模型无法使用，请更换模型",
    },
    ErrorCode.AGENT_NOT_FOUND: {
        "type": ErrorType.RESOURCE_NOT_FOUND,
        "message": "Agent does not exist or has been deleted",
        "user_message": "智能体不存在，可能已被删除",
    },
    ErrorCode.PROJECT_NOT_FOUND: {
        "type": ErrorType.RESOURCE_NOT_FOUND,
        "message": "Project knowledge base does not exist",
        "user_message": "知识库项目不存在",
    },
    ErrorCode.CONNECTOR_NOT_FOUND: {
        "type": ErrorType.RESOURCE_NOT_FOUND,
        "message": "Connector does not exist",
        "user_message": "连接器配置已被移除",
    },
    ErrorCode.SESSION_NOT_FOUND: {
        "type": ErrorType.RESOURCE_NOT_FOUND,
        "message": "Session does not exist",
        "user_message": "会话上下文已失效，请发起新对话",
    },
    ErrorCode.KNOWLEDGE_BASE_NOT_FOUND: {
        "type": ErrorType.RESOURCE_NOT_FOUND,
        "message": "Knowledge base does not exist",
        "user_message": "知识库不存在",
    },
    ErrorCode.RESOURCE_PRIVATE: {
        "type": ErrorType.PERMISSION_ERROR,
        "message": "Resource is private, no access permission",
        "user_message": "无权访问该智能体 / 知识库",
    },
    ErrorCode.AGENT_NOT_PUBLISHED: {
        "type": ErrorType.PERMISSION_ERROR,
        "message": "Agent is in draft status, API calls not allowed",
        "user_message": "智能体尚未发布，无法通过接口调用",
    },
    ErrorCode.OPERATION_FORBIDDEN: {
        "type": ErrorType.PERMISSION_ERROR,
        "message": "Current role lacks permission for this operation",
        "user_message": "账号权限不足，请联系组织管理员",
    },
    ErrorCode.RATE_LIMIT_REACHED: {
        "type": ErrorType.RATE_LIMIT_ERROR,
        "message": "API call QPS has reached the rate limit threshold",
        "user_message": "请求过于频繁，请稍后重试",
    },
    ErrorCode.MONTHLY_QUOTA_EXHAUSTED: {
        "type": ErrorType.QUOTA_EXCEEDED,
        "message": "Monthly token quota exhausted",
        "user_message": "Token 额度已用尽，请升级套餐或等待次月重置",
    },
    ErrorCode.DAILY_QUOTA_EXHAUSTED: {
        "type": ErrorType.QUOTA_EXCEEDED,
        "message": "Daily call quota exhausted",
        "user_message": "今日调用额度已用完，请明日继续使用",
    },
    ErrorCode.MODEL_TIMEOUT: {
        "type": ErrorType.MODEL_ERROR,
        "message": "Model service response timeout",
        "user_message": "AI 响应超时，请精简问题重试",
    },
    ErrorCode.MODEL_CONTENT_POLICY: {
        "type": ErrorType.MODEL_ERROR,
        "message": "Content triggered safety policy restriction",
        "user_message": "输入内容不符合安全规范，请调整提问内容",
    },
    ErrorCode.CONTEXT_OVERFLOW: {
        "type": ErrorType.MODEL_ERROR,
        "message": "Total context length exceeds model window limit",
        "user_message": "对话内容过长，请新建会话重试",
    },
    ErrorCode.SEARCH_FAILED: {
        "type": ErrorType.TOOL_EXECUTION_ERROR,
        "message": "Web search tool call failed",
        "user_message": "联网检索暂时不可用",
    },
    ErrorCode.DEEP_RESEARCH_FAIL: {
        "type": ErrorType.TOOL_EXECUTION_ERROR,
        "message": "Deep research task execution error",
        "user_message": "深度调研任务执行失败，请简化需求重试",
    },
    ErrorCode.CONNECTOR_AUTH_EXPIRED: {
        "type": ErrorType.TOOL_EXECUTION_ERROR,
        "message": "Connector authorization has expired",
        "user_message": "第三方连接器授权失效，请重新授权",
    },
    ErrorCode.FILE_PARSE_FAILED: {
        "type": ErrorType.TOOL_EXECUTION_ERROR,
        "message": "File parse failed, file corrupted or unsupported format",
        "user_message": "文件解析失败，请确认文件格式并重传",
    },
    ErrorCode.INTERNAL_ERROR: {
        "type": ErrorType.INTERNAL_SERVER_ERROR,
        "message": "Internal server error",
        "user_message": "系统临时故障，请稍后重试",
    },
    ErrorCode.INJECTION_DETECTED: {
        "type": ErrorType.PERMISSION_ERROR,
        "message": "Potential prompt injection detected in input",
        "user_message": "输入内容触发安全检测，请调整提问方式",
    },
}


@dataclass
class ErrorResponse:
    code: str = ""
    type: str = ""
    message: str = ""
    user_message: str = ""
    param: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "code": self.code,
            "type": self.type,
            "message": self.message,
            "user_message": self.user_message,
        }
        if self.param is not None:
            d["param"] = self.param
        return d

    @classmethod
    def from_error_code(cls, code: ErrorCode, param: str | None = None) -> ErrorResponse:
        entry = _ERROR_REGISTRY.get(code.value, {})
        return cls(
            code=code.value,
            type=entry.get("type", ErrorType.INTERNAL_SERVER_ERROR.value),
            message=entry.get("message", "Unknown error"),
            user_message=entry.get("user_message", "未知错误"),
            param=param,
        )


def raise_api_error(code: ErrorCode, param: str | None = None, detail: str | None = None):
    from fastapi import HTTPException

    err = ErrorResponse.from_error_code(code, param=param)
    if detail:
        err.message = detail

    status_map = {
        ErrorType.AUTH_ERROR: 401,
        ErrorType.INVALID_REQUEST_ERROR: 400,
        ErrorType.RESOURCE_NOT_FOUND: 404,
        ErrorType.PERMISSION_ERROR: 403,
        ErrorType.RATE_LIMIT_ERROR: 429,
        ErrorType.QUOTA_EXCEEDED: 429,
        ErrorType.MODEL_ERROR: 502,
        ErrorType.TOOL_EXECUTION_ERROR: 500,
        ErrorType.INTERNAL_SERVER_ERROR: 500,
    }
    status_code = status_map.get(ErrorType(err.type), 500)
    logger.warning("API error: code=%s type=%s param=%s", code.value, err.type, param)
    raise HTTPException(status_code=status_code, detail={"error": err.to_dict()})

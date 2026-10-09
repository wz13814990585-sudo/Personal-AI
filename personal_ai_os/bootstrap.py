"""Build the single-user service with process configuration and SQLite state."""

from dataclasses import replace
import os

import streamlit as st

from .agent_registry import AgentRegistry
from .agents import AgnoAgentRunner, MissingDeepSeekRunner
from .config import load_config
from .custom_agent_store import CustomAgentStore
from .external import ExternalGateway
from .harness import Harness
from .ntfy_notifications import notifier_from_environment
from .services import PersonalAIService
from .storage import Repository
from .tool_gateway import ToolGateway


@st.cache_resource
def _repository(database_path: str) -> Repository:
    repository = Repository(database_path)
    repository.initialize()
    repository.fail_interrupted_runs()
    CustomAgentStore(repository).fail_interrupted_runs()
    ExternalGateway(repository).recover_expired_attempts()
    return repository


def build_service() -> PersonalAIService:
    config = load_config(require_model=False)
    repository = _repository(str(config.database_path))
    timezone = repository.get_setting("timezone", config.timezone)
    config = replace(config, timezone=timezone)
    registry = AgentRegistry(repository)
    gateway = ToolGateway(repository, registry, config.timezone)
    runner = AgnoAgentRunner(config, registry) if config.deepseek_api_key else MissingDeepSeekRunner()
    harness = Harness(repository, registry, gateway, runner, config)
    return PersonalAIService(repository, gateway, harness, registry,
                             remote_notifier=notifier_from_environment())


def page_service() -> PersonalAIService:
    service = build_service()
    if service.demo_mode:
        st.info("演示模式：使用独立示例数据；真实日历、邮箱、YouTube 与远端通知已关闭。")
    if not service.model_ready:
        st.warning("未配置 DEEPSEEK_API_KEY：可管理本机数据；Agent 规划和反馈提取需要配置密钥。")
    return service

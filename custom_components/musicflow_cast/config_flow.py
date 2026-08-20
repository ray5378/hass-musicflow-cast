"""MusicFlow Cast 配置流程:填写 MusicFlow 地址 + 用户名 + 密码。

凭据用 **OpenSubsonic 标准** 的用户名 + 密码(不是 MusicFlow 专用 API Key):
流的 URL 需要 md5(password + salt) 算 token,API Key 做不到,所以这里直接收密码。
校验:用 OpenSubsonic ping 带着 u/t/s 打一次,status==ok 即通过。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MusicFlowAuthError, MusicFlowClient, MusicFlowError
from .const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _normalize_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    if not url:
        return url
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if parsed.port is None and parsed.scheme == "http" and parsed.hostname:
        url = f"http://{parsed.hostname}:{DEFAULT_PORT}"
    return url


class MusicFlowCastConfigFlow(ConfigFlow, domain=DOMAIN):
    """配置流程。"""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_url: str | None = None
        self._discovered_name: str | None = None

    async def _async_validate(self, url: str, username: str, password: str, verify_ssl: bool) -> tuple[dict[str, str], dict[str, Any]]:
        """返回 (errors, info)。errors 为空表示校验通过。"""
        try:
            session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
            client = MusicFlowClient(session, url, username, password, verify_ssl)
            info = await client.async_verify()
        except MusicFlowAuthError:
            return {"base": "invalid_auth"}, {}
        except MusicFlowError as err:
            _LOGGER.debug("MusicFlow 连通性校验失败: %s", err)
            return {"base": "cannot_connect"}, {}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("MusicFlow 连通性校验出现未预期异常")
            return {"base": "unknown"}, {}
        return {}, info

    def _schema(self, url_default: str = "", user_default: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_URL, default=url_default): str,
                vol.Required(CONF_USERNAME, default=user_default): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_VERIFY_SSL, default=True): bool,
            }
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        url_default = user_default = ""
        if user_input is not None:
            try:
                url = _normalize_url(user_input[CONF_URL])
                url_default = url
                username = user_input[CONF_USERNAME].strip()
                user_default = username
                password = user_input[CONF_PASSWORD]
                verify_ssl = user_input.get(CONF_VERIFY_SSL, True)
                errors, info = await self._async_validate(url, username, password, verify_ssl)
                if not errors:
                    await self.async_set_unique_id(url, raise_on_progress=False)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=self._title(info, url),
                        data={
                            CONF_URL: url,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_VERIFY_SSL: verify_ssl,
                        },
                    )
            except AbortFlow:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("配置流 user 步骤出现未预期异常")
                errors = {"base": "unknown"}

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(url_default, user_default),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        url = ""
        try:
            entry = self._get_reauth_entry()
            url = entry.data[CONF_URL]
            if user_input is not None:
                username = user_input[CONF_USERNAME].strip()
                password = user_input[CONF_PASSWORD]
                verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
                errors, _info = await self._async_validate(url, username, password, verify_ssl)
                if not errors:
                    return self.async_update_reload_and_abort(
                        entry, data_updates={CONF_USERNAME: username, CONF_PASSWORD: password}
                    )
        except AbortFlow:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.exception("配置流 reauth_confirm 步骤出现未预期异常")
            errors = {"base": "unknown"}

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"url": url},
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    @staticmethod
    def _title(info: dict[str, Any], url: str) -> str:
        username = info.get("username") or ""
        return f"MusicFlow Cast ({username})" if username else f"MusicFlow Cast ({url})"

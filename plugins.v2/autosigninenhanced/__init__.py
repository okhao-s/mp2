import os
import random
import re
import time
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import pytz
from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap
from bs4 import BeautifulSoup

from .cf_helper import (
    KIND_LABEL,
    KIND_LAYOUT,
    KIND_LOGIN,
    KIND_NETWORK,
    KIND_OK,
    KIND_SHIELD,
    RETRYABLE,
    ClearanceStore,
    FlareSolverr,
    classify as cf_classify,
    classify_message,
    cookies_to_string,
    extract_clearance,
    fmt_failure,
    is_challenge,
    merge_cookies,
    site_domain,
)


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def check_attendance(html_str) -> Tuple[bool, str]:
    soup = BeautifulSoup(html_str, 'html.parser')
    html_text = soup.text
    
    # 如果含 已签到
    if "已签到" in html_text:
        return True, "已签到"

    # 提取 签到已得\d+的魔力值
    sign_result_bonus = re.search(r'签到已得(\d+)', soup.text)
    
    if sign_result_bonus:
        sign_value = sign_result_bonus.group(1)
        success_msg = f"签到已得{sign_value}"
        return True, success_msg
    
    result = re.search(
            r'第(\d+)次\S+?已连续\S+?(\d+)天\S+?获得(\d+)', html_text)


    if result:
        sign_days = result.group(1)
        sign_total_days = result.group(2)
        sign_bonus = result.group(3)
        
        success_msg = f"第 {sign_days} 次签到，连续 {sign_total_days} 天，本次签到获得 {sign_bonus} 个魔力值"
        return True, success_msg

    # 使用列表推导式获取文本包含“本次签到获得”的父标签文本
    results = [
        re.sub(r'\s+', ' ', tag.parent.text.strip())
        for tag in soup.find_all(string=re.compile("本次|这次"))
    ]

    if len(results) > 0:
        sign_result = results[0]
        # 提取上述内容，使用正则提取
        result = re.search(
            r'\S+\s(\d+)\s\S+\s(\d+)\s天\S+\s(\d+)\s', sign_result)
        if result:
            sign_count, continuous_sign_count, sign_value = result.groups()
            success_msg = f"第 {sign_count} 次签到，连续 {continuous_sign_count} 天，本次签到获得 {sign_value} 个魔力值"
            return True, success_msg
        else:
            # 正则当前日期, 例如2025-01-01
            # 获取当前日期
            result = re.search(datetime.now().strftime("%Y-%m-%d") + r".*?获得魔力值：(\d+).*?连续天数：(\d+).*", soup.text)
            if result:
                sign_value, continuous_sign_count = result.groups()
                success_msg = f"本次签到获得 {sign_value} 个魔力值，连续签到 {continuous_sign_count} 天"
                return True, success_msg
            else:
                logger.warn(f"未找到签到结果：{sign_result}")
        return False, soup.text
    else:
        # print("未找到签到结果")
        return False, soup.text


class AutoSignInEnhanced(_PluginBase):
    # 插件名称
    plugin_name = "站点自动签到增强"
    # 插件描述
    plugin_desc = "自动模拟登录、签到站点。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "2.9.1-Enhanced-v0.3"
    # 插件作者
    plugin_author = "thsrite,cancelpt"
    # 作者主页
    author_url = "https://github.com/cancelpt"
    # 插件配置项ID前缀
    plugin_config_prefix = "autosigninenhanced_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 5
    _sign_sites: list = []
    _login_sites: list = []
    _retry_keyword = None
    _clean: bool = False
    _start_time: int = None
    _end_time: int = None
    _auto_cf: int = 0

    # CF 三层降级 + 并发控制
    _flare_url: str = ""
    _flare_timeout: int = 60
    _flare_proxy: str = ""
    _concurrency: int = 2
    _jitter: int = 3
    _retry_times: int = 2
    _clearance_ttl: int = 1800

    def init_plugin(self, config: dict = None):

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt") or 5
            self._sign_sites = config.get("sign_sites") or []
            self._login_sites = config.get("login_sites") or []
            self._retry_keyword = config.get("retry_keyword")
            self._auto_cf = config.get("auto_cf")
            self._clean = config.get("clean")

            # CF 三层降级 + 并发
            self._flare_url = (config.get("flare_url") or "").strip()
            self._flare_timeout = _int_or(config.get("flare_timeout"), 60)
            self._flare_proxy = (config.get("flare_proxy") or "").strip()
            self._concurrency = max(1, _int_or(config.get("concurrency"), 2))
            self._jitter = max(0, _int_or(config.get("jitter"), 3))
            self._retry_times = max(0, _int_or(config.get("retry_times"), 2))
            self._clearance_ttl = _int_or(config.get("clearance_ttl"), 1800)

            # 过滤掉已删除的站点
            all_sites = [site.id for site in SiteOper().list_order_by_pri()] + [site.get("id") for site in
                                                                                self.__custom_sites()]
            self._sign_sites = [site_id for site_id in all_sites if site_id in self._sign_sites]
            self._login_sites = [site_id for site_id in all_sites if site_id in self._login_sites]
            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            self._site_schema = ModuleHelper.load('app.plugins.autosigninenhanced.sites',
                                                  filter_func=lambda _, obj: hasattr(obj, 'match'))

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点自动签到服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "queue_cnt": self._queue_cnt,
                "sign_sites": self._sign_sites,
                "login_sites": self._login_sites,
                "retry_keyword": self._retry_keyword,
                "auto_cf": self._auto_cf,
                "clean": self._clean,
                "flare_url": self._flare_url,
                "flare_timeout": self._flare_timeout,
                "flare_proxy": self._flare_proxy,
                "concurrency": self._concurrency,
                "jitter": self._jitter,
                "retry_times": self._retry_times,
                "clearance_ttl": self._clearance_ttl,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/site_signin",
            "event": EventType.PluginAction,
            "desc": "站点签到",
            "category": "站点",
            "data": {
                "action": "site_signin"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [{
            "path": "/signin_by_domain",
            "endpoint": self.signin_by_domain,
            "methods": ["GET"],
            "summary": "站点签到",
            "description": "使用站点域名签到站点",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            try:
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "AutoSignInEnhanced",
                        "name": "站点自动签到增强服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.sign_in,
                        "kwargs": {}
                    }]
                else:
                    # 2.3/9-23
                    crons = str(self._cron).strip().split("/")
                    if len(crons) == 2:
                        # 2.3
                        cron = crons[0]
                        # 9-23
                        times = crons[1].split("-")
                        if len(times) == 2:
                            # 9
                            self._start_time = int(times[0])
                            # 23
                            self._end_time = int(times[1])
                        if self._start_time and self._end_time:
                            return [{
                                "id": "AutoSignInEnhanced",
                                "name": "站点自动签到增强服务",
                                "trigger": "interval",
                                "func": self.sign_in,
                                "kwargs": {
                                    "hours": float(str(cron).strip()),
                                }
                            }]
                        else:
                            logger.error("站点自动签到增强服务启动失败，周期格式错误")
                    else:
                        # 默认0-24 按照周期运行
                        return [{
                            "id": "AutoSignInEnhanced",
                            "name": "站点自动签到增强服务",
                            "trigger": "interval",
                            "func": self.sign_in,
                            "kwargs": {
                                "hours": float(str(self._cron).strip()),
                            }
                        }]
            except Exception as err:
                logger.error(f"定时任务配置错误：{str(err)}")
        elif self._enabled:
            # 随机时间
            triggers = TimerUtils.random_scheduler(num_executions=2,
                                                   begin_hour=9,
                                                   end_hour=23,
                                                   max_interval=6 * 60,
                                                   min_interval=2 * 60)
            ret_jobs = []
            for trigger in triggers:
                ret_jobs.append({
                    "id": f"AutoSignInEnhanced|{trigger.hour}:{trigger.minute}",
                    "name": "站点自动签到增强服务",
                    "trigger": "cron",
                    "func": self.sign_in,
                    "kwargs": {
                        "hour": trigger.hour,
                        "minute": trigger.minute
                    }
                })
            return ret_jobs
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in SiteOper().list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clean',
                                            'label': '清理本日缓存',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'queue_cnt',
                                            'label': '队列数量（旧字段，已由并发数替代）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_keyword',
                                            'label': '重试关键词',
                                            'placeholder': '支持正则表达式，命中才重签'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'auto_cf',
                                            'label': '自动优选',
                                            'placeholder': '命中重试关键词次数（0-关闭）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'CF 三层降级：直连 → FlareSolverr（自动缓存 cf_clearance，按站点复用）→ 浏览器仿真。'
                                                    '直连能过就不走 FlareSolverr，省时间也少触发风控。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'flare_url',
                                            'label': 'FlareSolverr 地址',
                                            'placeholder': 'http://flaresolverr:8191（留空则跳过该层）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'flare_timeout',
                                            'label': '解盾超时(秒)',
                                            'placeholder': '60'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'flare_proxy',
                                            'label': '解盾代理',
                                            'placeholder': 'http://ip:port（机房 IP 过不了强盾时填）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'concurrency',
                                            'label': '并发数',
                                            'placeholder': '2（建议 1-2，太高易触发风控）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'jitter',
                                            'label': '随机抖动(秒)',
                                            'placeholder': '3（每站 0~N 秒随机延迟）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'retry_times',
                                            'label': '失败重试次数',
                                            'placeholder': '2（仅网络/盾类失败重试）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'clearance_ttl',
                                            'label': 'cf_clearance 缓存(秒)',
                                            'placeholder': '1800'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'sign_sites',
                                            'label': '签到站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'login_sites',
                                            'label': '登录站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '执行周期支持：'
                                                    '1、5位cron表达式；'
                                                    '2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；'
                                                    '3、周期不填默认9-23点随机执行2次。'
                                                    '每天首次全量执行，其余执行命中重试关键词的站点。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '自动优选：0-关闭，命中重试关键词次数大于该数量时自动执行Cloudflare IP优选（需要开启且则正确配置Cloudflare IP优选插件和自定义Hosts插件）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '不是所有的站点都会把程序自动登录/签到定义为用户活跃（比如馒头），提示签到/登录成功仍然存在掉号风险！请结合站点公告说明自行把握。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "",
            "auto_cf": 0,
            "onlyonce": False,
            "clean": False,
            "queue_cnt": 5,
            "sign_sites": [],
            "login_sites": [],
            "retry_keyword": "错误|失败",
            "flare_url": "",
            "flare_timeout": 60,
            "flare_proxy": "",
            "concurrency": 2,
            "jitter": 3,
            "retry_times": 2,
            "clearance_ttl": 1800
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 获取最近14天的日期数组
        date_list = [(datetime.now() - timedelta(days=i)).date() for i in range(14)]

        # 获取所有数据，包括签到和登录历史
        all_data = {
            "signin": [],  # 签到数据
            "login": []  # 登录数据
        }
        sign_dates = set()
        sites_info = self._build_sites_info()

        # 获取常规日期格式数据
        for day in date_list:
            day_str = self._date_label(day)
            day_formatted = day.strftime('%Y-%m-%d')

            # 获取"月日"格式数据
            day_data = self.get_data(day_str)
            if day_data:
                # 添加日期信息到每条记录
                if isinstance(day_data, list):
                    for record in day_data:
                        if isinstance(record, dict):
                            record["date"] = day_str
                            record["day_obj"] = day
                            # 区分签到和登录数据
                            if "登录" in record.get("status", ""):
                                all_data["login"].append(record)
                            else:
                                all_data["signin"].append(record)
                    sign_dates.add(day_str)

            # 获取"签到-yyyy-mm-dd"和"登录-yyyy-mm-dd"格式数据
            signin_history = self.get_data(key="签到-" + day_formatted)
            if signin_history:
                if isinstance(signin_history, dict):
                    # 获取完成签到的站点ID列表
                    done_sites = signin_history.get("do", [])
                    retry_sites = signin_history.get("retry", [])

                    # 为所有已完成签到的站点创建记录
                    for site_id in done_sites:
                        site_name = self._get_site_display_name(site_id, sites_info)

                        # 跳过需要重试的站点
                        if site_id in retry_sites:
                            # 为需要重试的站点添加记录
                            status_text = "需要重试"
                            all_data["signin"].append({
                                "site": site_name,
                                "status": status_text,
                                "date": day_str,
                                "day_obj": day,
                                "site_id": site_id
                            })
                        else:
                            # 为已完成的站点添加记录
                            status_text = "已签到"
                            all_data["signin"].append({
                                "site": site_name,
                                "status": status_text,
                                "date": day_str,
                                "day_obj": day,
                                "site_id": site_id
                            })

                    sign_dates.add(day_str)

            # 获取登录历史数据
            login_history = self.get_data(key="登录-" + day_formatted)
            if login_history:
                if isinstance(login_history, dict):
                    # 获取完成登录的站点ID列表
                    done_sites = login_history.get("do", [])
                    retry_sites = login_history.get("retry", [])

                    # 为所有已完成登录的站点创建记录
                    for site_id in done_sites:
                        site_name = self._get_site_display_name(site_id, sites_info)

                        # 跳过需要重试的站点
                        if site_id in retry_sites:
                            # 为需要重试的站点添加记录
                            status_text = "登录需要重试"
                            all_data["login"].append({
                                "site": site_name,
                                "status": status_text,
                                "date": day_str,
                                "day_obj": day,
                                "site_id": site_id
                            })
                        else:
                            # 为已完成的站点添加记录
                            status_text = "登录成功"
                            all_data["login"].append({
                                "site": site_name,
                                "status": status_text,
                                "date": day_str,
                                "day_obj": day,
                                "site_id": site_id
                            })

                    sign_dates.add(day_str)

        # 如果没有数据，显示提示信息
        if not all_data["signin"] and not all_data["login"]:
            return [{
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'text': '暂无签到数据',
                    'variant': 'tonal',
                    'class': 'mt-4',
                    'prepend-icon': 'mdi-information'
                }
            }]

        # 确保签到数据中至少有所有日期的记录
        if sign_dates:
            sign_dates_list = list(sign_dates)
            sign_dates_list.sort(reverse=True)  # 最新日期优先
        else:
            sign_dates_list = [f"{date_list[0].month}月{date_list[0].day}日"]

        # 按站点分组并去重数据
        signin_site_data = {}
        login_site_data = {}

        # 处理签到数据 - 每个站点每天只保留一条最新记录
        site_day_records = {}  # 用于去重: {site}_{date} -> record
        for data in all_data["signin"]:
            site_name = data.get("site", "未知站点")
            date_str = data.get("date", "")
            site_day_key = f"{site_name}_{date_str}"

            # 存储或更新记录（如有多条取最新）
            if site_day_key in site_day_records:
                existing = site_day_records[site_day_key]
                if existing.get("message") and not data.get("message"):
                    if data.get("site_id") and not existing.get("site_id"):
                        existing["site_id"] = data.get("site_id")
                    continue
            site_day_records[site_day_key] = data

        # 整理去重后的数据
        for key, record in site_day_records.items():
            site_name = record.get("site", "未知站点")
            if site_name not in signin_site_data:
                signin_site_data[site_name] = []
            signin_site_data[site_name].append(record)

        # 处理登录数据 - 同样去重
        site_day_records = {}  # 重置去重字典
        for data in all_data["login"]:
            site_name = data.get("site", "未知站点")
            date_str = data.get("date", "")
            site_day_key = f"{site_name}_{date_str}"

            # 存储或更新记录
            if site_day_key in site_day_records:
                existing = site_day_records[site_day_key]
                if existing.get("message") and not data.get("message"):
                    if data.get("site_id") and not existing.get("site_id"):
                        existing["site_id"] = data.get("site_id")
                    continue
            site_day_records[site_day_key] = data

        # 整理去重后的数据
        for key, record in site_day_records.items():
            site_name = record.get("site", "未知站点")
            if site_name not in login_site_data:
                login_site_data[site_name] = []
            login_site_data[site_name].append(record)

        today_label = self._date_label(date_list[0])
        signin_stats = self._calculate_day_stats(signin_site_data, today_label)
        login_stats = self._calculate_day_stats(login_site_data, today_label)
        signin_issue_count = signin_stats["error"] + signin_stats["warning"]
        login_issue_count = login_stats["error"] + login_stats["warning"]
        issue_count = signin_issue_count + login_issue_count
        failure_count = signin_stats["error"] + login_stats["error"]
        retry_count = signin_stats["warning"] + login_stats["warning"]

        signin_color = "success"
        if signin_stats["error"]:
            signin_color = "error"
        elif signin_issue_count or signin_stats["none"]:
            signin_color = "warning"

        login_color = "success"
        if login_stats["error"]:
            login_color = "error"
        elif login_issue_count or login_stats["none"]:
            login_color = "warning"

        summary_items = [
            {
                "label": "今日签到",
                "value": f"{signin_stats['success']}/{signin_stats['total']}",
                "meta": f"异常 {signin_issue_count} · 未记录 {signin_stats['none']}",
                "color": signin_color,
                "icon": "mdi-calendar-check",
            },
            {
                "label": "异常重试",
                "value": str(issue_count),
                "meta": f"失败 {failure_count} · 重试 {retry_count}",
                "color": "error" if failure_count else "warning" if retry_count else "success",
                "icon": "mdi-alert-circle-outline",
            },
            {
                "label": "今日登录",
                "value": f"{login_stats['success']}/{login_stats['total']}",
                "meta": f"异常 {login_issue_count} · 未记录 {login_stats['none']}",
                "color": login_color,
                "icon": "mdi-login-variant",
            },
            {
                "label": "历史范围",
                "value": f"{len(sign_dates_list)} 天",
                "meta": f"显示 {len(sign_dates_list)} 天数据",
                "color": "info",
                "icon": "mdi-history",
            },
        ]
        summary_cards = []
        for item in summary_items:
            summary_cards.append({
                'component': 'VCol',
                'props': {
                    'cols': 6,
                    'sm': 3,
                },
                'content': [
                    {
                        'component': 'VCard',
                        'props': {
                            'variant': 'tonal',
                            'color': item['color'],
                            'class': 'summary-card',
                        },
                        'content': [
                            {
                                'component': 'VCardText',
                                'props': {
                                    'class': 'pa-3',
                                },
                                'content': [
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'summary-card__label',
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'icon': item['icon'],
                                                    'size': 'x-small',
                                                    'class': 'mr-1',
                                                },
                                            },
                                            {
                                                'component': 'span',
                                                'text': item['label'],
                                            },
                                        ],
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'summary-card__value',
                                        },
                                        'text': item['value'],
                                    },
                                    {
                                        'component': 'div',
                                        'props': {
                                            'class': 'summary-card__meta',
                                        },
                                        'text': item['meta'],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            })

        # 创建签到折叠面板
        signin_panels = []
        for site_name, records in sorted(
                signin_site_data.items(),
                key=lambda item: self._site_sort_key(item[0], item[1], date_list)):
            # 按日期排序，最新的在前面
            try:
                records.sort(key=lambda x: x.get("day_obj", datetime.now().date()), reverse=True)
            except Exception as e:
                logger.debug(f"排序失败: {str(e)}")

            # 获取最新的状态作为站点概要
            latest_record = self._latest_record(records)
            latest_status = latest_record.get("message") or latest_record.get("status", "未知状态")
            status_meta = self._status_meta(latest_record.get("status") or latest_status)

            # 创建每个站点的折叠面板
            signin_panels.append(
                self._create_expansion_panel(
                    site_name,
                    records,
                    status_meta["panel_color"],
                    status_meta["icon"],
                    latest_status,
                ))

        # 创建登录折叠面板
        login_panels = []
        for site_name, records in sorted(
                login_site_data.items(),
                key=lambda item: self._site_sort_key(item[0], item[1], date_list)):
            # 按日期排序，最新的在前面
            try:
                records.sort(key=lambda x: x.get("day_obj", datetime.now().date()), reverse=True)
            except Exception as e:
                logger.debug(f"排序失败: {str(e)}")

            latest_record = self._latest_record(records)
            latest_status = latest_record.get("message") or latest_record.get("status", "未知状态")
            status_meta = self._status_meta(latest_record.get("status") or latest_status)

            # 创建每个站点的折叠面板
            login_panels.append(
                self._create_expansion_panel(
                    site_name,
                    records,
                    status_meta["panel_color"],
                    status_meta["icon"],
                    latest_status,
                ))

        # 添加样式
        return [
            {
                'component': 'style',
                'text': """
                .v-expansion-panel-title {
                    min-height: 48px !important;
                    padding: 0 16px !important;
                }
                .v-expansion-panel-text__wrapper {
                    padding: 0 !important;
                }
                .v-expansion-panel {

                    margin-bottom: 10px !important;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
                    border-radius: 16px !important;
                    overflow: hidden !important;
                    border: 1px solid rgba(0,0,0,0.03);
                    transition: all 0.3s ease;
                }
                .v-expansion-panel:hover {

                    box-shadow: 0 4px 12px rgba(0,0,0,0.08) !important;
                    transform: translateY(-2px);
                }
                .site-item {
                    border-radius: 10px;
                    transition: all 0.3s ease;
                    margin: 5px 0;

                }
                .site-item:hover {

                    transform: scale(1.01);
                    box-shadow: 0 2px 8px rgba(0,0,0,0.05);
                }
                .text-teal-lighten-3 {
                    color: #80CBC4 !important;
                }
                .text-deep-orange-lighten-3 {
                    color: #FFAB91 !important;
                }
                .text-pink-lighten-3 {
                    color: #F8BBD0 !important;
                }
                .text-amber-lighten-3 {
                    color: #FFE082 !important;
                }
                .text-light-blue-lighten-3 {
                    color: #81D4FA !important;
                }
                .status-icon {
                    width: 24px;
                    height: 24px;
                    line-height: 24px;
                    text-align: center;
                    border-radius: 50%;
                    margin-right: 8px;
                }
                .signin-card, .login-card {
                    transition: all 0.3s ease;
                    border-radius: 20px !important;
                    overflow: hidden;
                    box-shadow: 0 4px 15px rgba(0,0,0,0.03) !important;
                    border: 1px solid rgba(0,0,0,0.03);
                }
                .signin-card:hover, .login-card:hover {
                    transform: translateY(-3px);
                    box-shadow: 0 6px 20px rgba(0,0,0,0.05) !important;
                }
                .v-card-title.gradient-title {
                    margin-bottom: 0 !important;
                    border-bottom: 1px solid rgba(0,0,0,0.03);
                }
                .signin-card .v-card-title.gradient-title {
                    background: linear-gradient(135deg, rgba(128, 203, 196, 0.15) 0%, rgba(165, 214, 167, 0.15) 100%);
                }
                .login-card .v-card-title.gradient-title {
                    background: linear-gradient(135deg, rgba(129, 212, 250, 0.15) 0%, rgba(159, 168, 218, 0.15) 100%);
                }
                .date-chip {
                    margin: 2px !important;
                    border-radius: 14px !important;
                    font-size: 0.75rem !important;
                }
                .status-chip {
                    padding: 0 8px;
                    border-radius: 14px !important;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.03);
                }
                .site-icon {
                    background: linear-gradient(45deg, #80CBC4, #81D4FA);
                    color: white !important;
                    border-radius: 12px;
                    width: 32px;
                    height: 32px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    margin-right: 10px;
                    font-weight: bold;
                    font-size: 15px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.06);
                }
                .page-title {
                    font-size: 1.5rem;
                    font-weight: 600;
                    background: -webkit-linear-gradient(45deg, #80CBC4, #81D4FA);
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }
                .summary-card {
                    min-height: 108px;
                }
                .summary-card__label {
                    display: flex;
                    align-items: center;
                    font-size: 0.75rem;
                    font-weight: 600;
                }
                .summary-card__value {
                    margin-top: 8px;
                    font-size: 1.25rem;
                    font-weight: 700;
                    line-height: 1;
                }
                .summary-card__meta {
                    margin-top: 6px;
                    font-size: 0.72rem;
                    opacity: 0.8;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }
                """
            },
            {
                'component': 'VRow',
                'props': {
                    'class': 'mt-2'
                },
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'class': 'pb-0'
                        },
                        'content': [
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'd-flex align-center mb-4'
                                },
                                'content': [
                                    {
                                        'component': 'VIcon',
                                        'props': {
                                            'color': 'light-blue-lighten-3',
                                            'class': 'mr-2',
                                            'size': 'large',
                                            'icon': 'mdi-cat'
                                        }
                                    },
                                    {
                                        'component': 'h2',
                                        'props': {
                                            'class': 'page-title m-0'
                                        },
                                        'text': '站点签到小助手'
                                    },
                                    {
                                        'component': 'VSpacer'
                                    },
                                    {
                                        'component': 'VChip',
                                        'props': {
                                            'color': 'light-blue-lighten-5',
                                            'size': 'small',
                                            'variant': 'elevated',
                                            'class': 'ml-2',
                                            'prepend-icon': 'mdi-paw'
                                        },
                                        'text': f'显示 {len(sign_dates_list)} 天数据'
                                    }
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'VRow',
                'props': {
                    'class': 'mb-1',
                },
                'content': summary_cards,
            },
            {
                'component': 'VRow',
                'content': [
                    # 左侧 - 签到数据
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'flat',
                                    'class': 'mb-4 signin-card'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'gradient-title d-flex align-center pa-4'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'class': 'mr-2',
                                                    'color': 'teal-lighten-3',
                                                    'size': 'small',
                                                    'icon': 'mdi-duck'
                                                }
                                            },
                                            {
                                                'component': 'span',
                                                'props': {
                                                    'class': 'font-weight-medium'
                                                },
                                                'text': '签到打卡记录'
                                            },
                                            {
                                                'component': 'VSpacer'
                                            },
                                            {
                                                'component': 'VChip',
                                                'props': {
                                                    'color': 'teal-lighten-5',
                                                    'size': 'x-small',
                                                    'variant': 'elevated',
                                                    'class': 'ml-2',
                                                    'prepend-icon': 'mdi-rabbit'
                                                },
                                                'text': f'{len(signin_site_data)} 个站点'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'content': [
                                            {
                                                'component': 'VExpansionPanels',
                                                'props': {
                                                    'variant': 'accordion',
                                                    'class': 'mt-2'
                                                },
                                                'content': signin_panels or [{
                                                    'component': 'VAlert',
                                                    'props': {
                                                        'type': 'info',
                                                        'text': '暂无签到数据',
                                                        'variant': 'tonal',
                                                        'class': 'mt-2',
                                                        'density': 'compact',
                                                        'prepend-icon': 'mdi-penguin'
                                                    }
                                                }]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    # 右侧 - 登录数据
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                            'md': 6
                        },
                        'content': [
                            {
                                'component': 'VCard',
                                'props': {
                                    'variant': 'flat',
                                    'class': 'mb-4 login-card'
                                },
                                'content': [
                                    {
                                        'component': 'VCardTitle',
                                        'props': {
                                            'class': 'gradient-title d-flex align-center pa-4'
                                        },
                                        'content': [
                                            {
                                                'component': 'VIcon',
                                                'props': {
                                                    'class': 'mr-2',
                                                    'color': 'light-blue-accent-3',
                                                    'size': 'small',
                                                    'icon': 'mdi-dog'
                                                }
                                            },
                                            {
                                                'component': 'span',
                                                'props': {
                                                    'class': 'font-weight-medium'
                                                },
                                                'text': '登录记录'
                                            },
                                            {
                                                'component': 'VSpacer'
                                            },
                                            {
                                                'component': 'VChip',
                                                'props': {
                                                    'color': 'light-blue-lighten-4',
                                                    'size': 'x-small',
                                                    'variant': 'elevated',
                                                    'class': 'ml-2',
                                                    'prepend-icon': 'mdi-panda'
                                                },
                                                'text': f'{len(login_site_data)} 个站点'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VCardText',
                                        'props': {
                                            'class': 'pa-3'
                                        },
                                        'content': [
                                            {
                                                'component': 'VExpansionPanels',
                                                'props': {
                                                    'variant': 'accordion',
                                                    'class': 'mt-2'
                                                },
                                                'content': login_panels or [{
                                                    'component': 'VAlert',
                                                    'props': {
                                                        'type': 'info',
                                                        'text': '暂无登录数据',
                                                        'variant': 'tonal',
                                                        'class': 'mt-2',
                                                        'density': 'compact',
                                                        'prepend-icon': 'mdi-cat'
                                                    }
                                                }]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

    @staticmethod
    def _add_site_info(sites_info: dict, site_id: Any, site_name: Any) -> None:
        """Store both native and string site IDs for historical lookups."""
        if site_id is None or not site_name:
            return
        site_name = str(site_name).strip()
        if not site_name:
            return
        sites_info[site_id] = site_name
        sites_info[str(site_id)] = site_name

    def _build_sites_info(self) -> dict:
        """Collect all site names that can appear in stored sign-in history."""
        sites_info = {}
        for site in SitesHelper().get_indexers():
            if not site.get("public"):
                self._add_site_info(
                    sites_info,
                    site.get("id"),
                    site.get("name"),
                )
        for site in SiteOper().list_order_by_pri():
            if isinstance(site, dict):
                site_id = site.get("id")
                site_name = site.get("name")
            else:
                site_id = getattr(site, "id", None)
                site_name = getattr(site, "name", None)
            self._add_site_info(sites_info, site_id, site_name)
        for site in self.__custom_sites():
            if isinstance(site, dict):
                site_id = site.get("id")
                site_name = site.get("name")
            else:
                site_id = getattr(site, "id", None)
                site_name = getattr(site, "name", None)
            self._add_site_info(sites_info, site_id, site_name)
        return sites_info

    @staticmethod
    def _get_site_display_name(site_id: Any, sites_info: dict) -> str:
        """Resolve a historical site ID while retaining the enhanced fallback."""
        return sites_info.get(str(site_id)) or sites_info.get(site_id) or f"站点ID: {site_id}"

    @staticmethod
    def _status_meta(status_text: str) -> dict:
        """Map a status message to a shared display level, icon, and ordering weight."""
        status_text = str(status_text or "").strip()
        if "Cookie已失效" in status_text or "失效" in status_text:
            return {
                "level": "error",
                "color": "error",
                "panel_color": "pink-lighten-3",
                "icon": "mdi-cookie-off",
                "sort": 0,
            }
        if "失败" in status_text or "错误" in status_text:
            return {
                "level": "error",
                "color": "error",
                "panel_color": "deep-orange-lighten-3",
                "icon": "mdi-alert-circle",
                "sort": 0,
            }
        if "重试" in status_text:
            return {
                "level": "warning",
                "color": "warning",
                "panel_color": "amber-lighten-3",
                "icon": "mdi-refresh",
                "sort": 1,
            }
        if "已签到" in status_text:
            return {
                "level": "success",
                "color": "info",
                "panel_color": "light-blue-lighten-3",
                "icon": "mdi-check",
                "sort": 3,
            }
        if "成功" in status_text:
            return {
                "level": "success",
                "color": "success",
                "panel_color": "teal-lighten-3",
                "icon": "mdi-check-circle",
                "sort": 3,
            }
        if status_text:
            return {
                "level": "none",
                "color": "grey",
                "panel_color": "grey-lighten-1",
                "icon": "mdi-help-circle-outline",
                "sort": 2,
            }
        return {
            "level": "none",
            "color": "grey",
            "panel_color": "grey-lighten-1",
            "icon": "mdi-minus-circle-outline",
            "sort": 2,
        }

    @staticmethod
    def _latest_record(records: list, date_label: str = None) -> dict:
        """Return the newest history record, optionally constrained to one day."""
        filtered_records = records
        if date_label:
            filtered_records = [record for record in records if record.get("date") == date_label]
        if not filtered_records:
            return {}
        try:
            return sorted(
                filtered_records,
                key=lambda record: record.get("day_obj", datetime.min.date()),
                reverse=True,
            )[0]
        except Exception as e:
            logger.debug(f"获取最新记录失败: {str(e)}")
            return filtered_records[0]

    @staticmethod
    def _date_label(day) -> str:
        """Format a date using the historic storage key displayed by this page."""
        return f"{day.month}月{day.day}日"

    @classmethod
    def _calculate_day_stats(cls, site_data: dict, date_label: str) -> dict:
        """Count successful, retrying, failing, and absent records for one day."""
        stats = {
            "total": len(site_data),
            "recorded": 0,
            "success": 0,
            "warning": 0,
            "error": 0,
            "none": 0,
        }
        for records in site_data.values():
            record = cls._latest_record(records, date_label)
            if not record:
                stats["none"] += 1
                continue
            stats["recorded"] += 1
            status_text = record.get("status") or record.get("message", "")
            level = cls._status_meta(status_text).get("level")
            if level in stats:
                stats[level] += 1
        return stats

    @classmethod
    def _site_sort_key(cls, site_name: str, records: list, display_dates: list) -> tuple:
        """Show current-day failures and retries before successful detail panels."""
        today_label = cls._date_label(display_dates[0]) if display_dates else ""
        today_record = cls._latest_record(records, today_label)
        latest_record = cls._latest_record(records)
        status_text = ""
        if today_record:
            status_text = today_record.get("status") or today_record.get("message", "")
        status_meta = cls._status_meta(status_text)
        latest_day = latest_record.get("day_obj") if latest_record else None
        try:
            latest_ordinal = latest_day.toordinal() if latest_day else datetime.min.date().toordinal()
        except Exception:
            latest_ordinal = datetime.min.date().toordinal()
        return status_meta["sort"], -latest_ordinal, str(site_name)

    @classmethod
    def _create_expansion_panel(cls, site_name, records, status_color, status_icon, latest_status):
        """创建站点折叠面板"""
        # 生成站点图标（使用站点名的首字母）
        site_initial = site_name[0].upper() if site_name else "?"

        # 生成记录列表
        records_list = []
        for record in records:
            date_str = record.get("date", "")
            status_text = record.get("status", "未知状态")

            record_meta = cls._status_meta(status_text or record.get("message", ""))
            record_color = record_meta["color"]
            record_icon = record_meta["icon"]

            # 创建记录项
            records_list.append({
                'component': 'VListItem',
                'props': {
                    'class': 'site-item px-2 py-1'
                },
                'content': [
                    {
                        'component': 'div',
                        'props': {
                            'class': 'd-flex align-center w-100'
                        },
                        'content': [
                            {
                                'component': 'VChip',
                                'props': {
                                    'color': 'grey-lighten-3',
                                    'size': 'x-small',
                                    'class': 'date-chip mr-2',
                                    'variant': 'flat',
                                    'prepend-icon': 'mdi-flower-tulip'
                                },
                                'text': date_str
                            },
                            {
                                'component': 'VSpacer'
                            },
                            {
                                'component': 'VChip',
                                'props': {
                                    'color': record_color,
                                    'size': 'x-small',
                                    'class': 'ml-2 status-chip',
                                    'variant': 'flat',
                                    'prepend-icon': record_icon
                                },
                                'text': f"{record.get('message', '')}" if record.get('message', '') else status_text
                            }
                        ]
                    }
                ]
            })

        # 创建折叠面板
        return {
            'component': 'VExpansionPanel',
            'content': [
                {
                    'component': 'VExpansionPanelTitle',
                    'content': [{
                        'component': 'div',
                        'props': {
                            'class': 'd-flex align-center'
                        },
                        'content': [
                            {
                                'component': 'div',
                                'props': {
                                    'class': 'site-icon'
                                },
                                'text': site_initial
                            },
                            {
                                'component': 'span',
                                'props': {
                                    'class': 'font-weight-medium'
                                },
                                'text': site_name
                            },
                            {
                                'component': 'VSpacer'
                            },
                            {
                                'component': 'VIcon',
                                'props': {
                                    'color': status_color,
                                    'class': 'mr-2',
                                    'size': 'small'
                                },
                                'text': status_icon
                            },
                            {
                                'component': 'span',
                                'props': {
                                    'class': f'text-{status_color} text-caption'
                                },
                                'text': latest_status
                            }
                        ]
                    }]
                },
                {
                    'component': 'VExpansionPanelText',
                    'content': [
                        {
                            'component': 'VList',
                            'props': {
                                'lines': 'one',
                                'density': 'compact'
                            },
                            'content': records_list
                        }
                    ]
                }
            ]
        }

    @eventmanager.register(EventType.PluginAction)
    def sign_in(self, event: Event = None):
        """
        自动签到|模拟登录
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "site_signin":
                return
        # 日期
        today = datetime.today()
        if self._start_time and self._end_time:
            if int(datetime.today().hour) < self._start_time or int(datetime.today().hour) > self._end_time:
                logger.error(
                    f"当前时间 {int(datetime.today().hour)} 不在 {self._start_time}-{self._end_time} 范围内，暂不执行任务")
                return
        if event:
            logger.info("收到命令，开始站点签到 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始站点签到 ...",
                              userid=event.event_data.get("user"))

        if self._sign_sites:
            self.__do(today=today, type_str="签到", do_sites=self._sign_sites, event=event)
        if self._login_sites:
            self.__do(today=today, type_str="登录", do_sites=self._login_sites, event=event)

    def __do(self, today: datetime, type_str: str, do_sites: list, event: Event = None):
        """
        签到逻辑
        """
        # 查看今天有没有签到|登录历史
        today_str = today.strftime('%Y-%m-%d')
        today_history = self.get_data(key=type_str + "-" + today_str)

        # 查询所有站点
        all_sites = [site for site in SitesHelper().get_indexers() if not site.get("public")] + self.__custom_sites()
        # 过滤掉没有选中的站点
        if do_sites:
            do_sites = [site for site in all_sites if site.get("id") in do_sites]
        else:
            do_sites = all_sites

        # 今日没数据
        if not today_history or self._clean:
            logger.info(f"今日 {today} 未{type_str}，开始{type_str}已选站点")
            if self._clean:
                # 关闭开关
                self._clean = False
        else:
            # 需要重试站点
            retry_sites = today_history.get("retry") or []
            # 今天已签到|登录站点
            already_sites = today_history.get("do") or []

            # 今日未签|登录站点
            no_sites = [site for site in do_sites if
                        site.get("id") not in already_sites or site.get("id") in retry_sites]

            if not no_sites:
                logger.info(f"今日 {today} 已{type_str}，无重新{type_str}站点，本次任务结束")
                return

            # 任务站点 = 需要重试+今日未do
            do_sites = no_sites
            logger.info(f"今日 {today} 已{type_str}，开始重试命中关键词站点")

        if not do_sites:
            logger.info(f"没有需要{type_str}的站点")
            return

        # 执行签到（并发受限 + 每站随机抖动，避免同时登录触发风控）
        logger.info(f"开始执行{type_str}任务，并发 {self._concurrency}，抖动 0~{self._jitter}s ...")
        worker_cnt = max(1, min(len(do_sites), int(self._concurrency or 2)))
        if type_str == "签到":
            with ThreadPool(worker_cnt) as p:
                status = p.map(self.signin_site, do_sites)
        else:
            with ThreadPool(worker_cnt) as p:
                status = p.map(self.login_site, do_sites)

        if status:
            logger.info(f"站点{type_str}任务完成！")
            # 获取今天的日期
            key = f"{datetime.now().month}月{datetime.now().day}日"
            today_data = self.get_data(key)
            new_records = []
            for s in status:
                msg = s[1]
                st = msg
                if "Cookie" in msg:
                    st = "Cookie已失效"
                elif "失败" in msg or "错误" in msg:
                    st = "失败"
                elif "已签到" in msg:
                    st = "已签到"
                elif "模拟登录成功" in msg:
                    st = "登录成功"
                elif "签到" in msg and ("得" in msg or "第" in msg):
                    st = "签到成功"
                elif "成功" in msg:
                    st = "成功"

                new_records.append({
                    "site": s[0],
                    "status": st,
                    "message": msg
                })

            if today_data:
                if not isinstance(today_data, list):
                    today_data = [today_data]
                today_data.extend(new_records)
            else:
                today_data = new_records
            # 保存数据
            self.save_data(key, today_data)

            # 命中重试词的站点id
            retry_sites = []
            # 命中重试词的站点签到msg
            retry_msg = []
            # 登录成功
            login_success_msg = []
            # 签到成功
            sign_success_msg = []
            # 已签到
            already_sign_msg = []
            # 仿真签到成功
            fz_sign_msg = []
            # 失败｜错误
            failed_msg = []

            sites = {site.get('name'): site.get("id") for site in SitesHelper().get_indexers() if
                     not site.get("public")}
            for s in status:
                site_name = s[0]
                site_id = None
                if site_name:
                    site_id = sites.get(site_name)

                if 'Cookie已失效' in str(s) and site_id:
                    # 触发自动登录插件登录
                    logger.info(f"触发站点 {site_name} 自动登录更新Cookie和Ua")
                    self.eventmanager.send_event(EventType.PluginAction,
                                                 {
                                                     "site_id": site_id,
                                                     "action": "site_refresh"
                                                 })
                # 记录本次命中重试关键词的站点
                if self._retry_keyword:
                    if site_id:
                        match = re.search(self._retry_keyword, s[1])
                        if match:
                            logger.debug(f"站点 {site_name} 命中重试关键词 {self._retry_keyword}")
                            retry_sites.append(site_id)
                            # 命中的站点
                            retry_msg.append(s)
                            continue

                if "登录成功" in str(s):
                    login_success_msg.append(s)
                elif "仿真签到成功" in str(s):
                    fz_sign_msg.append(s)
                    continue
                elif "签到成功" in str(s):
                    sign_success_msg.append(s)
                elif '已签到' in str(s):
                    already_sign_msg.append(s)
                else:
                    failed_msg.append(s)

            if not self._retry_keyword:
                # 没设置重试关键词则重试已选站点
                retry_sites = self._sign_sites if type_str == "签到" else self._login_sites
            logger.debug(f"下次{type_str}重试站点 {retry_sites}")

            # 存入历史
            self.save_data(key=type_str + "-" + today_str,
                           value={
                               "do": self._sign_sites if type_str == "签到" else self._login_sites,
                               "retry": retry_sites
                           })

            # 自动Cloudflare IP优选
            if self._auto_cf and int(self._auto_cf) > 0 and retry_msg and len(retry_msg) >= int(self._auto_cf):
                self.eventmanager.send_event(EventType.PluginAction, {
                    "action": "cloudflare_speedtest"
                })

            # 发送通知
            if self._notify:
                # 签到详细信息 登录成功、签到成功、已签到、仿真签到成功、失败--命中重试
                signin_message = login_success_msg + sign_success_msg + already_sign_msg + fz_sign_msg + failed_msg
                if len(retry_msg) > 0:
                    signin_message += retry_msg

                signin_message = "\n".join([f'【{s[0]}】{s[1]}' for s in signin_message if s])
                self.post_message(title=f"【站点自动{type_str}】",
                                  mtype=NotificationType.SiteMessage,
                                  text=f"全部{type_str}数量: {len(self._sign_sites if type_str == '签到' else self._login_sites)} \n"
                                       f"本次{type_str}数量: {len(do_sites)} \n"
                                       f"下次{type_str}数量: {len(retry_sites) if self._retry_keyword else 0} \n"
                                       f"{signin_message}"
                                  )
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"站点{type_str}完成！", userid=event.event_data.get("user"))
        else:
            logger.error(f"站点{type_str}任务失败！")
            if event:
                self.post_message(channel=event.event_data.get("channel"),
                                  title=f"站点{type_str}任务失败！", userid=event.event_data.get("user"))
        # 保存配置
        self.__update_config()

    def __build_class(self, url) -> Any:
        for site_schema in self._site_schema:
            try:
                if site_schema.match(url):
                    return site_schema
            except Exception as e:
                logger.error("站点模块加载失败：%s" % str(e))
        return None

    def signin_by_domain(self, url: str, apikey: str) -> schemas.Response:
        """
        签到一个站点，可由API调用
        """
        # 校验
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        domain = StringUtils.get_url_domain(url)
        site_info = SitesHelper().get_indexer(domain)
        if not site_info:
            return schemas.Response(
                success=True,
                message=f"站点【{url}】不存在"
            )
        else:
            site_name, message = self.signin_site(site_info)
            return schemas.Response(
                success=True,
                message=f"站点【{site_name}】{message or '签到成功'}"
            )

    def _clearance_store(self) -> ClearanceStore:
        """cf_clearance 缓存文件（按站点存，含 UA）"""
        try:
            base = str(self.get_data_path())
        except Exception:
            base = os.path.join(settings.CONFIG_PATH, "plugin_data", "autosigninenhanced")
        return ClearanceStore(os.path.join(base, "cf_clearance.json"), self._clearance_ttl)

    def _flare(self) -> FlareSolverr:
        return FlareSolverr(self._flare_url, self._flare_timeout)

    def _signin_once(self, site_info: CommentedMap) -> Tuple[bool, str, str]:
        """执行一次签到（适配器优先，否则走通用流程）"""
        site_module = self.__build_class(site_info.get("url"))
        if site_module and hasattr(site_module, "signin"):
            try:
                state, message = site_module().signin(site_info)
            except Exception as e:
                traceback.print_exc()
                return False, fmt_failure(KIND_NETWORK, str(e)), KIND_NETWORK
            return state, message, (KIND_OK if state else classify_message(message))
        return self.__signin_base(site_info)

    def signin_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        签到一个站点：每站随机抖动 + 失败分类 + 仅对网络/盾类失败自动重试
        """
        site_name = site_info.get("name")
        if self._jitter:
            time.sleep(random.uniform(0, float(self._jitter)))
        attempts = max(1, int(self._retry_times or 0) + 1)
        start_time = datetime.now()
        state, message, kind = False, "", KIND_LAYOUT
        for i in range(attempts):
            state, message, kind = self._signin_once(site_info)
            if state or kind not in RETRYABLE:
                break
            if i + 1 < attempts:
                backoff = min(30, 5 * (2 ** i)) + random.uniform(0, 3)
                logger.warning(f"{site_name} {KIND_LABEL.get(kind, kind)}，{backoff:.0f}s 后重试"
                               f"（第 {i + 1}/{attempts - 1} 次）")
                time.sleep(backoff)
        seconds = (datetime.now() - start_time).seconds
        domain = StringUtils.get_url_domain(site_info.get('url'))
        if state:
            SiteOper().success(domain=domain, seconds=seconds)
        else:
            SiteOper().fail(domain)
            logger.warning(f"{site_name} 签到失败（{KIND_LABEL.get(kind, kind)}，耗时 {seconds}s）：{message}")
        return site_name, message

    @staticmethod
    def __http_get(url: str, cookie: str, ua: str, proxies, timeout) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """直连取页面，返回 (html, status, error)"""
        try:
            res = RequestUtils(cookies=cookie, ua=ua, proxies=proxies, timeout=timeout).get_res(url=url)
        except Exception as e:
            return None, None, str(e)
        if res is None:
            return None, None, "请求无响应"
        return res.text, res.status_code, None

    @staticmethod
    def __judge(site: str, html: Optional[str], status: Optional[int], err: Optional[str],
                source: str = "") -> Tuple[bool, str, str]:
        """根据响应判断结果，并归类失败原因"""
        kind = cf_classify(html, status, err)
        if kind == KIND_NETWORK:
            return False, fmt_failure(KIND_NETWORK, err or "无法打开网站"), KIND_NETWORK
        if kind == KIND_SHIELD:
            return False, fmt_failure(KIND_SHIELD, "命中 Cloudflare 挑战页"), KIND_SHIELD
        if kind == KIND_LOGIN or not SiteUtils.is_logged_in(html or ""):
            return False, fmt_failure(KIND_LOGIN, "Cookie 已失效"), KIND_LOGIN
        sign_result, sign_msg = check_attendance(html or "")
        if sign_result:
            logger.info(f"{site} 签到结果：{sign_msg}（{source}）")
            return True, sign_msg, KIND_OK
        return False, fmt_failure(KIND_LAYOUT, "已登录但未匹配到签到结果，站点可能改版"), KIND_LAYOUT

    def __signin_base(self, site_info: CommentedMap) -> Tuple[bool, str, str]:
        """
        通用签到：直连 → FlareSolverr（缓存 cf_clearance）→ 浏览器仿真
        :return: (是否成功, 消息, 失败类别)
        """
        if not site_info:
            return False, "", KIND_LAYOUT
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        timeout = site_info.get("timeout") or 60
        if not site_url or not site_cookie:
            logger.warning(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return False, fmt_failure(KIND_LOGIN, "未配置站点地址或 Cookie"), KIND_LOGIN

        checkin_url = site_url
        if site_url.find("attendance.php") == -1:
            checkin_url = urljoin(site_url, "attendance.php")
        domain = site_domain(site_url)
        store = self._clearance_store()

        # ---------- 第 1 层：直连（带缓存 clearance） ----------
        cookie = site_cookie
        cached = store.get(domain)
        if cached:
            cookie = merge_cookies(site_cookie, cached["cookie"])
            logger.info(f"{site} 使用缓存 cf_clearance（剩余 {int(cached['expires'] - time.time())}s）")
        logger.info(f"开始站点签到：{site}，地址：{checkin_url}（直连）...")
        html, status, err = self.__http_get(checkin_url, cookie, ua, proxies, timeout)
        if not html and site_url != checkin_url:
            logger.info(f"{site} 签到页无响应，改访问首页：{site_url}")
            html, status, err = self.__http_get(site_url, cookie, ua, proxies, timeout)
        state, msg, kind = self.__judge(site, html, status, err, source="直连")
        if state:
            return state, msg, kind
        if kind == KIND_LOGIN and cached:
            # 缓存的 clearance 可能已失效，清掉下次重解
            store.invalidate(domain)
        if kind == KIND_NETWORK:
            return state, msg, kind
        if kind != KIND_SHIELD and not render:
            return state, msg, kind

        # ---------- 第 2 层：FlareSolverr ----------
        if kind == KIND_SHIELD and self._flare_url:
            logger.warning(f"{site} 直连被盾，尝试 FlareSolverr（{self._flare_url}）...")
            solved = self._flare().solve(
                url=checkin_url,
                cookies=cookie,
                proxy=self._flare_proxy or None,
                max_timeout_ms=max(20000, int(self._flare_timeout) * 1000),
            )
            if solved and solved.get("html"):
                clearance = extract_clearance(solved.get("cookies") or [])
                if clearance:
                    store.set(domain, clearance, solved.get("user_agent") or "")
                    logger.info(f"{site} 已缓存 cf_clearance（TTL {self._clearance_ttl}s）")
                state, msg, kind = self.__judge(site, solved.get("html"), solved.get("status") or 200,
                                                None, source="FlareSolverr")
                if state:
                    return state, msg, kind
                if kind == KIND_NETWORK:
                    return state, msg, kind
            else:
                logger.warning(f"{site} FlareSolverr 解盾失败（检查地址/代理/是否已启动）")

        # ---------- 第 3 层：浏览器仿真 ----------
        if render:
            logger.warning(f"{site} 尝试浏览器仿真兜底...")
            try:
                page_source = PlaywrightHelper().get_page_source(url=checkin_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server,
                                                                 timeout=timeout)
            except Exception as e:
                logger.warning(f"{site} 浏览器仿真异常：{e}")
                page_source = None
            if page_source:
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source) or is_challenge(page_source):
                        return False, fmt_failure(KIND_SHIELD, "直连/FlareSolverr/浏览器仿真均未通过"), KIND_SHIELD
                    return False, fmt_failure(KIND_LOGIN, "仿真登录失败，Cookie 已失效"), KIND_LOGIN
                sign_result, sign_msg = check_attendance(page_source)
                if sign_result:
                    logger.info(f"{site} 签到结果：{sign_msg}（浏览器仿真）")
                    return True, sign_msg, KIND_OK
                return False, fmt_failure(KIND_LAYOUT, "仿真已登录但未匹配到签到结果"), KIND_LAYOUT

        return state, msg, kind

    def login_site(self, site_info: CommentedMap) -> Tuple[str, str]:
        """
        模拟登录一个站点
        """
        site_module = self.__build_class(site_info.get("url"))
        # 开始记时
        start_time = datetime.now()
        if site_module and hasattr(site_module, "login"):
            try:
                state, message = site_module().login(site_info)
            except Exception as e:
                traceback.print_exc()
                state, message = False, f"模拟登录失败：{str(e)}"
        else:
            state, message = self.__login_base(site_info)
        # 统计
        seconds = (datetime.now() - start_time).seconds
        domain = StringUtils.get_url_domain(site_info.get('url'))
        if state:
            SiteOper().success(domain=domain, seconds=seconds)
        else:
            SiteOper().fail(domain)
        return site_info.get("name"), message

    @staticmethod
    def __login_base(site_info: CommentedMap) -> Tuple[bool, str]:
        """
        模拟登录通用处理
        :param site_info: 站点信息
        :return: 签到结果信息
        """
        if not site_info:
            return False, ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        timeout = site_info.get("timeout") or 60
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return False, ""
        # 模拟登录
        try:
            # 访问链接
            site_url = str(site_url).replace("attendance.php", "")
            logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=site_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server,
                                                                 timeout=timeout)
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return False, f"无法通过Cloudflare！"
                    return False, f"仿真登录失败，Cookie已失效！"
                else:
                    return True, "模拟登录成功"
            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies,
                                   timeout=timeout
                                   ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        logger.warn(f"{site} 模拟登录失败，{msg}")
                        return False, f"模拟登录失败，{msg}！"
                    else:
                        logger.info(f"{site} 模拟登录成功")
                        return True, f"模拟登录成功"
                elif res is not None:
                    logger.warn(f"{site} 模拟登录失败，状态码：{res.status_code}")
                    return False, f"模拟登录失败，状态码：{res.status_code}！"
                else:
                    logger.warn(f"{site} 模拟登录失败，无法打开网站")
                    return False, f"模拟登录失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 模拟登录失败：%s" % (site, str(e)))
            traceback.print_exc()
            return False, f"模拟登录失败：{str(e)}！"

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            self._sign_sites = self.__remove_site_id(config.get("sign_sites") or [], site_id)
            self._login_sites = self.__remove_site_id(config.get("login_sites") or [], site_id)
            # 保存配置
            self.__update_config()

    def __remove_site_id(self, do_sites, site_id):
        if do_sites:
            if isinstance(do_sites, str):
                do_sites = [do_sites]

            # 删除对应站点
            if site_id:
                do_sites = [site for site in do_sites if int(site) != int(site_id)]
            else:
                # 清空
                do_sites = []

            # 若无站点，则停止
            if len(do_sites) == 0:
                self._enabled = False

        return do_sites


def record_to_row(record):
    """辅助函数：将记录转换为表格行"""
    status = record.get("status", "")

    # 确定状态图标和颜色
    icon = "mdi-check-circle"
    color = "success"

    if "失败" in status or "错误" in status:
        icon = "mdi-alert-circle"
        color = "error"
    elif "Cookie已失效" in status:
        icon = "mdi-cookie-off"
        color = "error"
    elif "已签到" in status:
        icon = "mdi-check"
        color = "grey"
    elif "成功" in status:
        icon = "mdi-check-circle"
        color = "success"

    return {
        'component': 'tr',
        'props': {
            'class': 'text-sm'
        },
        'content': [
            {
                'component': 'td',
                'props': {
                    'class': 'text-start'
                },
                'text': record.get("date", "")
            },
            {
                'component': 'td',
                'props': {
                    'class': 'text-start'
                },
                'text': status
            },
            {
                'component': 'td',
                'props': {
                    'class': 'text-center'
                },
                'content': [
                    {
                        'component': 'VIcon',
                        'props': {
                            'color': color,
                            'size': 'small'
                        },
                        'text': icon
                    }
                ]
            }
        ]
    }

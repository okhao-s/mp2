from enum import Enum


class EventType(str, Enum):
    PluginAction = "plugin.action"
    SiteDeleted = "site.deleted"


class NotificationType(str, Enum):
    Plugin = "Plugin"
    SiteMessage = "SiteMessage"

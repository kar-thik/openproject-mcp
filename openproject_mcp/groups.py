"""Tool group registry (SPEC §3.2): the group tags and what they are called.

Every tool carries exactly one group tag, plus an optional sub-tag that names a
slice of its group (the recurring meeting tools carry ``meetings`` and
``meetings_recurring``). ``OPENPROJECT_MCP_DISABLE`` and
``OPENPROJECT_MCP_PROFILE`` key off these names, so they are public API.

This is a leaf module — it imports nothing from the package — so that both
:mod:`openproject_mcp.config` and :mod:`openproject_mcp.tools._shared` can use
it. Tool modules keep importing the constants through ``_shared``.
"""

from __future__ import annotations

__all__ = [
    "ALL_GROUPS",
    "CORE_PROFILE_HIDDEN_GROUPS",
    "GROUP_ATTACHMENTS",
    "GROUP_BUDGETS",
    "GROUP_DOCUMENTS",
    "GROUP_GIT",
    "GROUP_MEETINGS",
    "GROUP_MEETINGS_RECURRING",
    "GROUP_METADATA",
    "GROUP_NEWS",
    "GROUP_NOTIFICATIONS",
    "GROUP_PEOPLE",
    "GROUP_PROJECTS",
    "GROUP_QUERIES",
    "GROUP_REPORTING",
    "GROUP_TIME_ENTRIES",
    "GROUP_TITLES",
    "GROUP_VERSIONS",
    "GROUP_WIKI",
    "GROUP_WORK_PACKAGES",
    "GROUP_WP_COLLABORATION",
    "PARENT_GROUP",
]

GROUP_WORK_PACKAGES = "work_packages"
GROUP_WP_COLLABORATION = "wp_collaboration"
GROUP_ATTACHMENTS = "attachments"
GROUP_PROJECTS = "projects"
GROUP_METADATA = "metadata"
GROUP_GIT = "git_activity"
GROUP_QUERIES = "queries"
GROUP_NOTIFICATIONS = "notifications"
GROUP_TIME_ENTRIES = "time_entries"
GROUP_VERSIONS = "versions"
GROUP_PEOPLE = "people"
GROUP_MEETINGS = "meetings"
GROUP_MEETINGS_RECURRING = "meetings_recurring"
GROUP_NEWS = "news"
GROUP_DOCUMENTS = "documents"
GROUP_BUDGETS = "budgets"
GROUP_WIKI = "wiki"
GROUP_REPORTING = "reporting"

#: Group tag → the heading title the README uses for its tool table.
GROUP_TITLES: dict[str, str] = {
    GROUP_WORK_PACKAGES: "Work packages",
    GROUP_WP_COLLABORATION: "Comments, relations, watchers, reminders",
    GROUP_ATTACHMENTS: "Attachments and file links",
    GROUP_GIT: "Git and pull requests",
    GROUP_PROJECTS: "Projects",
    GROUP_QUERIES: "Saved queries",
    GROUP_NOTIFICATIONS: "Notifications",
    GROUP_TIME_ENTRIES: "Time tracking",
    GROUP_VERSIONS: "Versions and sprints",
    GROUP_PEOPLE: "People and memberships",
    GROUP_METADATA: "Instance metadata and schemas",
    GROUP_MEETINGS: "Meetings",
    GROUP_MEETINGS_RECURRING: "Recurring meetings",
    GROUP_WIKI: "Wiki",
    GROUP_DOCUMENTS: "Documents",
    GROUP_BUDGETS: "Budgets",
    GROUP_NEWS: "News",
    GROUP_REPORTING: "Reporting",
}

#: Every valid group tag, sub-tags included.
ALL_GROUPS: frozenset[str] = frozenset(GROUP_TITLES)

#: Sub-tag → the group it is a slice of. Disabling the parent hides the child.
PARENT_GROUP: dict[str, str] = {GROUP_MEETINGS_RECURRING: GROUP_MEETINGS}

#: Groups ``OPENPROJECT_MCP_PROFILE=core`` hides at startup: the module-backed
#: ones a typical work-package session never touches.
CORE_PROFILE_HIDDEN_GROUPS: frozenset[str] = frozenset(
    {
        GROUP_MEETINGS,
        GROUP_MEETINGS_RECURRING,
        GROUP_NEWS,
        GROUP_DOCUMENTS,
        GROUP_WIKI,
        GROUP_BUDGETS,
        GROUP_GIT,
        GROUP_REPORTING,
    }
)

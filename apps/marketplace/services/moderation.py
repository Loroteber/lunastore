"""Shared moderation logic for publish/edit requests.

The approval recipes used to live inline in the admin actions. They are moved
here so the very same code path can be triggered automatically right after a
request is created, for authors marked as trusted (``User.is_trusted``).

Every approval - manual or automatic - keeps the usual audit trail:

* ``django.contrib.admin.models.LogEntry`` is written with the ``status
  approved: ...`` message, which ``apps.core.signals.notify_on_admin_action``
  picks up and mirrors to the moderator Telegram chat;
* an extra, explicitly labelled Telegram message is sent for automatic
  approvals so moderators can tell them apart from their own actions;
* the author still receives the regular in-app notification.
"""

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils.html import escape
from safedelete.models import HARD_DELETE

import logging

from apps.core.logger.services import AUTO_APPROVAL_MARKER
from apps.core.tasks import send_notification, send_telegram_notification

from ..models import (
    Application,
    AppCreateRequests,
    AppEditRequests,
    Distribution,
    DistributionCreateRequests,
    DistributionEditRequests,
)


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"

APP_TRANS_FIELDS = ("title", "description", "requirements", "slogan")

DEFAULT_APPROVAL_LOG_MESSAGE = "status approved: Одобрена заявка"

APPROVAL_LOG_MESSAGES = {
    AppCreateRequests: "status approved: Одобрена заявка на приложение",
    AppEditRequests: "status approved: Одобрена заявка на изменение приложения",
    DistributionCreateRequests: "status approved: Одобрена заявка на дистрибуцию",
    DistributionEditRequests:
        "status approved: Одобрена заявка на изменение дистрибуции",
}

logger = logging.getLogger(__name__)


def is_trusted_user(user) -> bool:
    """True when the given user is allowed to skip human moderation."""
    if user is None:
        return False
    return bool(getattr(user, "is_trusted", False))


def _copy_app_translations(source, target) -> None:
    for field in APP_TRANS_FIELDS:
        for lang_code, _ in settings.LANGUAGES:
            lang_field = f"{field}_{lang_code}"
            setattr(target, lang_field, getattr(source, lang_field, None))


def _copy_changelog_translations(source, target, only_if_present=False) -> None:
    for lang_code, _ in settings.LANGUAGES:
        lang_field = f"changelog_{lang_code}"
        if only_if_present and not hasattr(source, lang_field):
            continue
        setattr(target, lang_field, getattr(source, lang_field, None))


def _mark_approved(req) -> None:
    req.status = STATUS_APPROVED
    req.save()


def _push_user_notification(req, title_key, content_key, context) -> None:
    send_notification.enqueue(
        user_id=req.user.id,
        title_key=title_key,
        content_key=content_key,
        context=context,
        meta={"icon": "help.png"},
    )


def _log_approval(req, actor=None, auto=False) -> LogEntry:
    """Write the admin log entry that gets mirrored to Telegram.

    ``actor`` is the moderator for manual approvals and the author themselves
    for automatic ones. ``auto`` appends a marker so the Telegram log can
    distinguish the two.
    """
    change_message = APPROVAL_LOG_MESSAGES.get(
        type(req),
        DEFAULT_APPROVAL_LOG_MESSAGE)

    if auto:
        change_message = f"{change_message} ({AUTO_APPROVAL_MARKER}: доверенный автор)"

    return LogEntry.objects.create(
        user_id=actor.id if actor is not None else None,
        content_type_id=ContentType.objects.get_for_model(req).pk,
        object_id=str(req.id),
        object_repr=str(req),
        action_flag=CHANGE,
        change_message=change_message,
    )


def _admin_change_link(obj) -> str:
    if obj is None or obj.pk is None:
        return ""

    try:
        url_path = reverse(
            f"admin:{obj._meta.app_label}_{obj._meta.model}_change",
            args=[obj.pk])
    except Exception:
        return ""

    base_domain = getattr(
        settings,
        "LUNASPIRE_URL_WITHOUT_PROTO",
        "lunastore.app")
    return f"https://{base_domain}{url_path}"


def _notify_auto_approval(req, auto, subject, details, target=None) -> None:
    """Explicit Telegram ping for approvals made without a moderator."""
    if not auto:
        return

    if not getattr(settings, "TELEGRAM_LOGGER_ENABLED", False):
        return

    username = escape(str(req.user.username)) if req.user else "Неизвестно"
    message = (
        f"⚡️ <b>{subject}</b>\n\n"
        f"👤 <b>Автор:</b> {username}\n"
        f"{details}\n"
        f"🤖 <b>Причина:</b> автор доверенный (is_trusted), заявка одобрена автоматически"
    )

    link = _admin_change_link(target)
    if link:
        message = (f"{message}\n"
                   f"🔗 <b>Ссылка:</b> <a href='{link}'>Перейти в админку</a>")

    send_telegram_notification(message)


def approve_app_create_request(req, actor=None, auto=False):
    """Materialize an ``Application`` out of an ``AppCreateRequests``.

    Returns the created ``Application``, or ``None`` when the request was
    already approved (or is otherwise not processable).
    """
    if req.status == STATUS_APPROVED:
        return None

    app = Application(
        user=req.user,
        price=req.price,
        icon_id=req.icon_id,
        icon_path=req.icon_path,
        screenshots=req.screenshots,
        developer_site=req.developer_site,
        original_author=req.original_author,
        is_demo=req.is_demo,
        is_private=req.is_private,
        allow_community_distributions=req.allow_community_distributions
    )
    _copy_app_translations(req, app)

    app.save()
    app.categories.set(req.categories.all())
    app.badges.set(req.badges.all())

    _mark_approved(req)
    _push_user_notification(
        req,
        "NOTIF_APPREQ_ACCEPTED_TITLE",
        "NOTIF_APPREQ_ACCEPTED_DESCRIPTION",
        {"app_name": app.title},
    )
    _log_approval(req, actor=actor, auto=auto)
    _notify_auto_approval(
        req,
        auto,
        subject="Автоодобрена заявка на публикацию приложения",
        details=f"📱 <b>Приложение:</b> {escape(str(app.title))}",
        target=app,
    )

    req.delete()
    return app


def approve_app_edit_request(req, actor=None, auto=False):
    """Apply an ``AppEditRequests`` to its target application.

    Returns the updated ``Application``, or ``None`` when the request was
    already approved or the target application is gone.
    """
    if req.status == STATUS_APPROVED:
        return None

    app = req.target_application
    if app is None:
        return None

    app.categories.set(req.categories.all())
    app.badges.set(req.badges.all())
    app.original_author = req.original_author
    app.price = req.price
    app.is_demo = req.is_demo
    app.developer_site = req.developer_site
    app.is_private = req.is_private
    app.allow_community_distributions = req.allow_community_distributions

    if req.icon_path:
        app.icon_id = req.icon_id
        app.icon_path = req.icon_path
    if req.screenshots is not None:
        app.screenshots = req.screenshots

    _copy_app_translations(req, app)
    app.save()

    _mark_approved(req)
    _push_user_notification(
        req,
        "NOTIF_APPEDITREQ_ACCEPTED_TITLE",
        "NOTIF_APPEDITREQ_ACCEPTED_DESCRIPTION",
        {"app_name": req.title},
    )
    _log_approval(req, actor=actor, auto=auto)
    _notify_auto_approval(
        req,
        auto,
        subject="Автоодобрена заявка на изменение приложения",
        details=f"📱 <b>Приложение:</b> {escape(str(req.title))}",
        target=app,
    )

    req.delete()
    return app


def approve_dist_create_request(req, actor=None, auto=False):
    """Materialize a ``Distribution`` out of a ``DistributionCreateRequests``.

    Returns the created ``Distribution``, or ``None`` when the request was
    already approved.
    """
    if req.status == STATUS_APPROVED:
        return None

    dist = Distribution(
        app=req.app,
        version=req.version,
        cdn_file_id=req.cdn_file_id,
        url=req.url,
    )
    dist.lunabox_manifest = req.lunabox_manifest
    _copy_changelog_translations(req, dist)

    dist.save()

    _mark_approved(req)
    _push_user_notification(
        req,
        "NOTIF_DISTREQ_ACCEPTED_TITLE",
        "NOTIF_DISTREQ_ACCEPTED_DESCRIPTION",
        {"app_name": req.app.title, "version": req.version},
    )
    _log_approval(req, actor=actor, auto=auto)
    _notify_auto_approval(
        req,
        auto,
        subject="Автоодобрена заявка на публикацию дистрибуции",
        details=(
            f"📱 <b>Приложение:</b> {escape(str(req.app.title))} "
            f"(v{escape(str(req.version))})"),
        target=dist,
    )

    req.delete(force_policy=HARD_DELETE)
    return dist


def approve_dist_edit_request(req, actor=None, auto=False):
    """Apply a ``DistributionEditRequests`` to its target distribution.

    Returns the updated ``Distribution``, or ``None`` when the request was
    already approved or the target distribution is gone.
    """
    if req.status == STATUS_APPROVED:
        return None

    dist = req.target_distribution
    if dist is None:
        return None

    dist.version = req.version
    dist.url = req.url
    dist.lunabox_manifest = req.lunabox_manifest

    if req.cdn_file_id:
        dist.cdn_file_id = req.cdn_file_id

    _copy_changelog_translations(req, dist, only_if_present=True)
    dist.save()

    _mark_approved(req)
    _push_user_notification(
        req,
        "NOTIF_DISTEDITREQ_ACCEPTED_TITLE",
        "NOTIF_DISTEDITREQ_ACCEPTED_DESCRIPTION",
        {"app_name": req.app.title, "version": req.version},
    )
    _log_approval(req, actor=actor, auto=auto)
    _notify_auto_approval(
        req,
        auto,
        subject="Автоодобрена заявка на изменение дистрибуции",
        details=(
            f"📱 <b>Приложение:</b> {escape(str(req.app.title))} "
            f"(v{escape(str(req.version))})"),
        target=dist,
    )

    req.delete(force_policy=HARD_DELETE)
    return dist


APPROVERS = {
    AppCreateRequests: approve_app_create_request,
    AppEditRequests: approve_app_edit_request,
    DistributionCreateRequests: approve_dist_create_request,
    DistributionEditRequests: approve_dist_edit_request,
}


def auto_approve_request(req):
    """Approve ``req`` right away when its author is a trusted one.

    Returns the created/updated ``Application``/``Distribution`` on success, or
    ``None`` when the request has to wait for a human moderator. Never raises:
    a failure to auto-approve must not break the submit form.
    """
    if getattr(req, "status", None) != STATUS_PENDING:
        return None

    if not is_trusted_user(getattr(req, "user", None)):
        return None

    approver = APPROVERS.get(type(req))
    if approver is None:
        return None

    try:
        return approver(req, actor=req.user, auto=True)
    except Exception:
        logger.exception(
            "Auto approval of %s #%s failed, "
            "falling back to manual moderation",
            type(req).__name__,
            req.pk,
        )
        return None

import os
import re
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from safedelete.models import SOFT_DELETE, SOFT_DELETE_CASCADE, SafeDeleteModel
from safedelete.queryset import SafeDeleteQueryset
from safedelete.managers import SafeDeleteManager
from django.utils.translation import get_language


def get_icon_path(instance, filename):
    # for application model
    ext = filename.split(".")[-1]
    filename = f"{uuid.uuid4().hex}.{ext}"
    return os.path.join("ugc/app_icons", filename)


class Category(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE_CASCADE

    name = models.CharField(max_length=80, verbose_name="Название")
    description = models.CharField(max_length=140, verbose_name="Описание")
    icon = models.CharField(
        max_length=140, null=True, blank=True, verbose_name="Иконка"
    )
    is_admin_only = models.BooleanField(
        default=False,
        verbose_name="Только для админов",
        help_text="Если включено, обычные пользователи не смогут выбрать эту категорию."
    )
    banner_filename = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name="Файл баннера",
        help_text="Название файла из папки staticfiles/img/categorybanner (например, 'custom.png')"
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Категория"
        verbose_name_plural = "Категории"

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"<Category {self.name}>"

    @property
    def banner_url(self) -> str | None:
        if not self.banner_filename:
            return None
        return f"/staticfiles/img/categorybanner/{self.banner_filename.lstrip('/')}"


class Badge(models.Model):
    PREDEFINED_STYLES = (
        ("custom", "Кастомный (цвета ниже)"),
        ("editor_choice", "Выбор редакции (синий фон, иконка звезды)"),
        ("verified", "Официальный издатель (зеленый фон, иконка глобуса)"),
        ("exclusive", "Эксклюзив (красный фон, !! вместо иконки)"),
    )

    name = models.CharField(max_length=80, verbose_name="Название")
    predefined_style = models.CharField(
        max_length=20,
        choices=PREDEFINED_STYLES,
        default="custom",
        verbose_name="Готовый стиль")

    # Поля для кастомного стиля
    icon_class = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        verbose_name="CSS класс иконки",
        help_text="Например: award, official. Применимо только для кастомного стиля.")
    icon_text = models.CharField(
        max_length=10, blank=True, null=True,
        verbose_name="Текст вместо иконки",
        help_text="Например: !!. Применимо только для кастомного стиля."
    )
    bg_color = models.CharField(
        max_length=7, default="#5fa359", verbose_name="Цвет фона",
        help_text="Только для кастомного стиля (в HEX)"
    )
    text_color = models.CharField(
        max_length=7, default="#ffffff", verbose_name="Цвет текста",
        help_text="Только для кастомного стиля (в HEX)"
    )
    border_color = models.CharField(
        max_length=7, default="#006000", verbose_name="Цвет границы",
        help_text="Только для кастомного стиля (в HEX)"
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Бейджик"
        verbose_name_plural = "Бейджики"

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"<Badge {self.name}>"


class BaseApplicationInfo(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE_CASCADE

    categories = models.ManyToManyField(
        "Category",
        verbose_name="Категории",
        blank=True,
        related_name="%(class)s_categories")
    badges = models.ManyToManyField(
        "Badge",
        verbose_name="Бейджики",
        blank=True,
        related_name="%(class)s_badges")
    title = models.CharField(max_length=80, verbose_name="Название")
    original_author = models.CharField(
        max_length=80,
        null=True,
        blank=True,
        verbose_name="Оригинальный автор",
        default="неизвестен",
    )
    description = models.CharField(max_length=1400, verbose_name="Описание")
    requirements = models.CharField(
        max_length=1400, null=True, verbose_name="Системные требования"
    )
    slogan = models.CharField(
        max_length=240, null=True, blank=True, verbose_name="Слоган"
    )
    icon_id = models.PositiveIntegerField(null=True, blank=True)
    icon_path = models.CharField(max_length=255, null=True, blank=True)
    price = models.IntegerField(default=0, verbose_name="Цена")
    screenshots = models.JSONField(
        default=list, blank=True, null=True, verbose_name="Скриншоты"
    )
    developer_site = models.URLField(
        max_length=160, null=True, blank=True, verbose_name="Сайт разработчика"
    )
    is_demo = models.BooleanField(default=False, verbose_name="Демо-версия")
    is_private = models.BooleanField(
        default=False, verbose_name="Приложение приватное?")
    allow_reviews = models.BooleanField(
        default=True, verbose_name="Разрешить отзывы")
    allow_community_distributions = models.BooleanField(
        default=False, verbose_name="Разрешить сторонние дистрибуции")

    class Meta:
        abstract = True

    @property
    def icon_url(self) -> str:
        if self.icon_path:
            from apps.core.local import get_geo_spire_url
            base_url = get_geo_spire_url(
                getattr(
                    settings,
                    "LUNASPIRE_URL",
                    "")).rstrip("/")
            protocol_relative_url = base_url.replace(
                "https:", "").replace("http:", "")

            path = self.icon_path.lstrip("/")
            return f"{protocol_relative_url}/{path}"
        return "/staticfiles/img/noavatar_64.jpg"

    @property
    def screenshot_urls(self) -> list[str]:
        from apps.core.local import get_geo_spire_url
        base_url = get_geo_spire_url(
            getattr(
                settings,
                "LUNASPIRE_URL",
                "")).rstrip("/")
        protocol_relative_url = base_url.replace(
            "https:", "").replace("http:", "")

        urls = []
        for path in self.screenshots or []:
            clean_path = path.lstrip("/")
            urls.append(f"{protocol_relative_url}/{clean_path}")
        return urls


class ApplicationQuerySet(SafeDeleteQueryset):
    def with_rating(self):
        from django.db.models import Avg
        return self.annotate(cached_avg_rating=Avg("reviews__rating"))


class ApplicationManager(SafeDeleteManager):
    _queryset_class = ApplicationQuerySet

    def with_rating(self):
        return self.get_queryset().with_rating()


class Application(BaseApplicationInfo):
    _safedelete_policy = SOFT_DELETE_CASCADE

    objects = ApplicationManager()

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name="Автор",
    )

    is_under_dmca = models.BooleanField(default=False)
    published = models.DateTimeField(auto_now=True)
    rating_cache = models.FloatField(
        default=0.0,
        db_index=True,
        verbose_name="Кэш среднего рейтинга",
    )
    reviews_count = models.PositiveIntegerField(
        default=0,
        verbose_name="Количество отзывов",
    )

    def update_rating_cache(self):
        from django.db.models import Avg, Count
        from .models import Review
        agg = self.reviews.filter(status=Review.STATUS_APPROVED).aggregate(avg=Avg("rating"), count=Count("id"))
        avg_val = round(agg["avg"] or 0.0, 1)
        count_val = agg["count"] or 0
        self.rating_cache = avg_val
        self.reviews_count = count_val
        Application.objects.all_with_deleted().filter(pk=self.pk).update(
            rating_cache=avg_val,
            reviews_count=count_val,
        )
        return avg_val

    @property
    def is_translated_to_current_lang(self):
        current_lang = get_language()
        field_name = f"title_{current_lang}"
        value = getattr(self, field_name, None)
        return bool(value)

    @property
    def avg_rating(self):
        if hasattr(self, "cached_avg_rating"):
            avg = self.cached_avg_rating
            return round(avg, 1) if avg else 0
        return self.rating_cache

    @property
    def star_class(self):
        avg = self.avg_rating
        if not avg:
            return ""

        rounded_val = round(avg * 2) / 2
        return "r" + str(rounded_val).replace(".5", "_5").replace(".0", "")

    class Meta:
        ordering = ["title"]
        verbose_name = "Приложение"
        verbose_name_plural = "Приложения"
        permissions = [
            ("set_dmca_flag", "Can set DMCA flag on application"),
            ("set_demo_flag", "Can set demo flag on application"),
        ]

    def __str__(self):
        return self.title


class BaseDistributionInfo(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    app = models.ForeignKey(
        'Application',
        on_delete=models.CASCADE,
        related_name="%(class)ss",
        # generate 'distributions' and 'distributionrequests'
        verbose_name="Приложение"
    )
    version = models.CharField(max_length=20, verbose_name="Версия")
    cdn_file_id = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="ID файла в CDN")
    url = models.URLField(
        max_length=140,
        null=True,
        blank=True,
        verbose_name="Внешняя ссылка (если не CDN)")
    changelog = models.CharField(
        max_length=210,
        verbose_name="Список изменений")
    lunabox_manifest = models.JSONField(
        verbose_name="Инструкция для лунной коробки как ставить приложение - {\"type\":\"program\",\"path\":\"\"}",
        default=None,
        null=True,
        blank=True,
        # None - manifest not configured yet (user should fill it via new distribution form)
        # types:
        # file - means, just download it and do nothing
        # installer - means it is installer and should be executed right after download
        # program - means it is already installed, and lunastore should use it directly without installing
        # path should be empty only if <type_of_file> is not in archive, for example:
        # lunabox downloaded tits.zip (this is example name for distribution)
        # and it sees that path is not empty, it checks if archive type supported (only zip rn)
        # and if supported it will extract it and
        # install folder1/inst.exe if path = "folder1/inst.exe" it is should be relative to archive
        # {"type":"program","path":""}
    )

    class Meta:
        abstract = True

    def __str__(self):
        return f"{self.app} {self.version}"

    def __repr__(self):
        # will be <Distribution AppName v1.0>
        return f"<{self.__class__.__name__} {self.app} {self.version}>"

    @property
    def has_download(self) -> bool:
        # check, can be downloaded file now
        return bool(self.cdn_file_id or self.url)

    @property
    def link(self) -> str:
        # route all downloads via internal view
        if self.has_download:
            return f"/get_dist_file/{self.pk}/"
        return "#"

    @property
    def is_external(self) -> bool:
        # check if file is hosted externally
        return not bool(self.cdn_file_id) and bool(self.url)

    @property
    def lunabox_manifest_is_filled(self) -> bool:
        # manifest is configured only when 'type' is known and set by the user
        manifest = self.lunabox_manifest or {}
        return bool(manifest.get("type"))

    @property
    def lunabox_type(self) -> str:
        manifest = self.lunabox_manifest or {}
        mtype = manifest.get("type", "file")
        path = manifest.get("path") or ""
        if mtype == "program":
            return "app_zip" if path else "program"
        if mtype == "installer":
            return "installer_zip" if path else "installer"
        return "file"

    @property
    def lunabox_path(self) -> str:
        manifest = self.lunabox_manifest or {}
        return manifest.get("path") or ""


class Distribution(BaseDistributionInfo):
    published = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["app", "-published"]
        verbose_name = "Дистрибуция"
        verbose_name_plural = "Дистрибуции"


class DistributionCreateRequests(BaseDistributionInfo):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dist_requests",
        verbose_name="Автор заявки"
    )

    # only for moderation
    virustotal_url = models.URLField(
        max_length=255, verbose_name="Ссылка на VirusTotal")
    cdn_hash = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="Хэш от LunaSpire")

    status_choices = (
        ("pending", "На рассмотрении"),
        ("approved", "Одобрено"),
        ("rejected", "Отклонено"),
    )
    status = models.CharField(
        max_length=20,
        choices=status_choices,
        default="pending",
        verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка на дистрибуцию"
        verbose_name_plural = "Заявки на дистрибуции"


class AppCreateRequests(BaseApplicationInfo, SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="create_requests",
        verbose_name="Автор заявки",
    )

    status_choices = (
        ("pending", "На рассмотрении"),
        ("approved", "Одобрено"),
        ("rejected", "Отклонено"),
    )
    status = models.CharField(
        max_length=20,
        choices=status_choices,
        default="pending",
        verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка на создание"
        verbose_name_plural = "Заявки на создание"

    def __str__(self):
        return f"Заявка #{self.id} на создание ({self.title})"


class DistributionEditRequests(BaseDistributionInfo):
    target_distribution = models.ForeignKey(
        'Distribution',
        on_delete=models.CASCADE,
        related_name="edit_requests",
        verbose_name="Редактируемая дистрибуция"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dist_edit_requests",
        verbose_name="Автор правки"
    )

    # for moderation
    virustotal_url = models.URLField(
        max_length=255,
        null=True,
        blank=True,
        verbose_name="Ссылка на VirusTotal")
    cdn_hash = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        verbose_name="Хэш от CDN")

    status_choices = (
        ("pending", "На рассмотрении"),
        ("approved", "Одобрено"),
        ("rejected", "Отклонено"),
    )
    status = models.CharField(
        max_length=20,
        choices=status_choices,
        default="pending",
        verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка на изменение дистрибуции"
        verbose_name_plural = "Заявки на изменения дистрибуций"

    def __str__(self):
        return f"Правка #{self.id} для {self.target_distribution.version}"


class AppEditRequests(BaseApplicationInfo, SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    target_application = models.ForeignKey(
        Application,
        on_delete=models.CASCADE,
        related_name="edit_requests",
        verbose_name="Редактируемое приложение",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="edit_requests_author",
        verbose_name="Автор правки",
    )
    status_choices = (
        ("pending", "На рассмотрении"),
        ("approved", "Одобрено"),
        ("rejected", "Отклонено"),
    )
    status = models.CharField(
        max_length=20,
        choices=status_choices,
        default="pending",
        verbose_name="Статус")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Заявка на изменение"
        verbose_name_plural = "Заявки на изменения"

    def __str__(self):
        app_title = self.target_application.title if self.target_application else "Удаленное приложение"
        return f"Заявка #{self.id} на изменение ({app_title})"


class AppReportRequests(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    REPORT_REASONS = [
        ("1", _("PAGE_REPORTAPP_REASON_MALWARE")),
        ("2", _("PAGE_REPORTAPP_REASON_INCORRECT")),
        ("3", _("PAGE_REPORTAPP_REASON_OTHER")),
    ]

    STATUS_CHOICES = [
        ("pending", "Новая"),
        ("resolved", "Рассматривается"),
        ("dismissed", "Отклонено"),
    ]

    app = models.ForeignKey(
        "Application", on_delete=models.CASCADE, related_name="reports"
    )

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        verbose_name=_("PAGE_REPORTAPP_AUTHOR"),
    )

    reason = models.CharField(
        max_length=1,
        choices=REPORT_REASONS,
        default="1")
    description = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="Статус")

    class Meta:
        verbose_name = _("PAGE_REPORTAPP_TITLE")
        verbose_name_plural = _("PAGE_REPORTAPP_TITLE_PATH")

    def __str__(self):
        app_title = self.app.title if self.app else "Неизвестное приложение"
        return f"Жалоба #{self.id} на {app_title}"


class ProblemReportRequests(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    STATUS_CHOICES = [
        ("pending", "Новая"),
        ("resolved", "Рассматривается"),
        ("dismissed", "Отклонено"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        verbose_name=_("PAGE_REPORTPROBLEM_AUTHOR"),
    )

    description = models.TextField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending",
        verbose_name="Статус")

    class Meta:
        verbose_name = "сообщение о проблеме"
        verbose_name_plural = "сообщений о проблеме"

    def __str__(self):
        return f"Жалоба #{self.id} на проблему"


class Review(models.Model):
    STATUS_APPROVED = "approved"
    STATUS_PENDING = "pending"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = (
        (STATUS_APPROVED, _("PAGE_APP_REVIEW_STATUS_APPROVED")),
        (STATUS_PENDING, _("PAGE_APP_REVIEW_STATUS_PENDING")),
        (STATUS_REJECTED, _("PAGE_APP_REVIEW_STATUS_REJECTED")),
    )

    application = models.ForeignKey(
        Application,
        on_delete=models.CASCADE,
        related_name="reviews",
        verbose_name="Приложение"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reviews",
        verbose_name="Пользователь"
    )
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        verbose_name="Оценка"
    )
    text = models.TextField(
        blank=True,
        default="",
        max_length=2000,
        verbose_name="Текст отзыва",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_APPROVED,
        db_default=STATUS_APPROVED,
        db_index=True,
        verbose_name="Статус",
    )
    ai_score = models.FloatField(
        null=True,
        blank=True,
        verbose_name="AI индекс нарушений (0.0 - 1.0)",
    )
    ai_flags = models.JSONField(
        default=list,
        blank=True,
        verbose_name="AI флаги нарушений",
    )
    ai_reason = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Причина вердикта ИИ",
    )
    ai_raw_response = models.JSONField(
        null=True,
        blank=True,
        verbose_name="Сырой ответ ИИ",
    )
    moderated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Дата модерации",
    )
    moderated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="moderated_reviews",
        verbose_name="Модератор",
    )
    moderation_note = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name="Заметка модератора",
    )
    developer_reply = models.TextField(
        blank=True,
        default="",
        max_length=2000,
        verbose_name="Ответ разработчика",
    )
    developer_reply_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Дата ответа разработчика",
    )
    developer_reply_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="developer_replies",
        verbose_name="Автор ответа",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name="Дата создания",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name="Дата обновления",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["application", "user"],
                name="unique_review_application_user",
            )
        ]
        indexes = [
            models.Index(fields=["application", "status"]),
        ]
        verbose_name = _("Отзыв")
        verbose_name_plural = _("Отзывы")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.application_id:
            self.application.update_rating_cache()

    def delete(self, *args, **kwargs):
        try:
            app = self.application
        except Exception:
            app = None
        super().delete(*args, **kwargs)
        if app:
            try:
                app.update_rating_cache()
            except Exception:
                pass

    def __str__(self):
        return f"Отзыв {self.rating}★ от {self.user} для {self.application.title}"


class PendingReview(Review):
    class Meta:
        proxy = True
        verbose_name = "Отзыв на модерации"
        verbose_name_plural = "Отзывы на модерации"


# user-owned app collection (system likes or custom)
class Collection(SafeDeleteModel):
    _safedelete_policy = SOFT_DELETE

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="collections",
        verbose_name="Владелец",
    )
    title = models.CharField(max_length=120, verbose_name="Название")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    is_system = models.BooleanField(
        default=False,
        verbose_name="Системная коллекция",
        help_text="system likes collection; at most one per owner",
    )
    is_public = models.BooleanField(default=True, verbose_name="Публичная")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        verbose_name = "Коллекция"
        verbose_name_plural = "Коллекции"
        constraints = [
            models.UniqueConstraint(
                fields=["owner"],
                condition=models.Q(is_system=True, deleted__isnull=True),
                name="uniq_system_collection_per_owner",
            ),
        ]
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return str(self.title)

    # return up to 'limit' application icon urls for the 2x2 mosaic
    def mosaic_icons(self, limit: int = 4) -> list[str]:
        icons: list[str] = []
        try:
            if hasattr(self, '_prefetched_objects_cache') and 'items' in self._prefetched_objects_cache:
                for item in self.items.all():
                    app = getattr(item, "application", None)
                    if app and not getattr(app, "deleted", None):
                        icons.append(app.icon_url)
                        if len(icons) >= limit:
                            break
            else:
                items = (
                    self.items.filter(application__deleted__isnull=True)
                    .select_related("application")
                    .order_by("-added_at")[:limit]
                )
                for item in items:
                    icons.append(item.application.icon_url)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "failed to build mosaic icons for collection id=%s", self.pk
            )
        while len(icons) < limit:
            icons.append("/staticfiles/img/noavatar_64.jpg")
        return icons[:limit]


# ensure the owner has a system likes collection with translated defaults
def get_or_create_likes_collection(user) -> Collection:
    from django.utils.translation import activate, get_language, gettext

    existing = Collection.objects.filter(owner=user, is_system=True).first()
    if existing is not None:
        return existing

    current_lang = get_language() or settings.LANGUAGE_CODE
    title_fields: dict[str, str] = {}
    desc_fields: dict[str, str] = {}
    try:
        for lang_code, _lang_name in settings.LANGUAGES:
            activate(lang_code)
            title_fields[f"title_{lang_code}"] = gettext(
                "PAGE_COLLECTION_LIKES_TITLE"
            )
            desc_fields[f"description_{lang_code}"] = gettext(
                "PAGE_COLLECTION_LIKES_DESC"
            )
    finally:
        activate(current_lang)

    create_kwargs: dict = {
        "owner": user,
        "is_system": True,
        "is_public": True,
        "title": title_fields.get(
            f"title_{settings.LANGUAGE_CODE}",
            "Понравившиеся программы",
        ),
        "description": desc_fields.get(
            f"description_{settings.LANGUAGE_CODE}",
            "",
        ),
    }
    create_kwargs.update(title_fields)
    create_kwargs.update(desc_fields)

    try:
        collection = Collection.objects.create(**create_kwargs)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "failed to create likes collection for user id=%s", getattr(user, "pk", None)
        )
        collection = Collection.objects.filter(owner=user, is_system=True).first()
        if collection is None:
            raise
    return collection


# application membership in a collection
class CollectionItem(models.Model):
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="Коллекция",
    )
    application = models.ForeignKey(
        Application,
        on_delete=models.CASCADE,
        related_name="collection_items",
        verbose_name="Приложение",
    )
    added_at = models.DateTimeField(auto_now_add=True, verbose_name="Добавлено")

    class Meta:
        verbose_name = "Элемент коллекции"
        verbose_name_plural = "Элементы коллекций"
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "application"],
                name="unique_collectionitem_collection_application",
            )
        ]
        ordering = ["-added_at"]

    def __str__(self) -> str:
        return f"{self.collection_id}:{self.application_id}"


# user favorite/save of another user's collection
class CollectionFavorite(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="favorite_collections",
        verbose_name="Пользователь",
    )
    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name="favorites",
        verbose_name="Коллекция",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата")

    class Meta:
        verbose_name = "Избранная коллекция"
        verbose_name_plural = "Избранные коллекции"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "collection"],
                name="unique_collectionfavorite_user_collection",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.collection_id}"


# admin-configured category blocks for the rich homepage (not API)
# managed in admin, rendered to users via index_rich.html
class HomeCategoryBlock(models.Model):
    category = models.ForeignKey(
        Category,
        on_delete=models.CASCADE,
        related_name="home_blocks",
        verbose_name="Категория",
    )
    sort_order = models.PositiveIntegerField(
        default=0,
        verbose_name="Порядок",
        help_text="Меньше значение — выше на главной",
    )
    is_enabled = models.BooleanField(default=True, verbose_name="Включён")
    apps_limit = models.PositiveSmallIntegerField(
        default=4,
        verbose_name="Кол-во приложений",
        help_text="Сколько карточек показывать в блоке (макс. 24)",
        validators=[
            MinValueValidator(1),
            MaxValueValidator(24),
        ],
    )

    class Meta:
        verbose_name = "Блок категории на главной"
        verbose_name_plural = "Блоки категорий на главной"
        ordering = ["sort_order", "id"]

    def __str__(self) -> str:
        return f"{self.category} (#{self.sort_order})"

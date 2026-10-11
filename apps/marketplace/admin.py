from unfold.contrib.forms.widgets import UnfoldAdminTextInputWidget
from unfold.widgets import UnfoldAdminColorInputWidget
from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.html import format_html
from urllib.parse import unquote
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from unfold import admin as unfold_admin
from unfold.decorators import action
from apps.core.tasks import send_notification
from safedelete.models import HARD_DELETE
from lunastore.mixins import SafeDeleteAdmin
from . import translation
from .forms import (
    ApplicationAdminForm,
    DistributionAdminForm,
    build_lunabox_manifest,
    get_translated_widgets_dict,
    LUNABOX_PATH_REQUIRED_TYPES,
    LUNABOX_TYPE_CHOICES,
)
from .widgets import UnfoldMarkdownTextareaWidget
from .models import *
from .services.moderation import (
    approve_app_create_request,
    approve_app_edit_request,
    approve_dist_create_request,
    approve_dist_edit_request,
)
from modeltranslation.admin import TabbedTranslationAdmin, TranslationTabularInline, TranslationStackedInline
import logging
import time
import jwt
from uuid import uuid4


logger = logging.getLogger(__name__)


def _build_direct_cdn_download_url(obj) -> str | None:
    if not obj.cdn_file_id:
        return None

    if not settings.LUNASPIRE_SECRET_KEY:
        logger.warning("missing LUNASPIRE_SECRET_KEY, cannot build direct CDN url")
        return None

    try:
        payload = {
            "type": "cdn-download",
            "file_id": int(obj.cdn_file_id),
            "exp": int(time.time()) + 600,
            "jti": uuid4().hex,
        }
        if getattr(obj, "app_id", None):
            payload["app_id"] = int(obj.app_id)

        download_token = jwt.encode(
            payload,
            settings.LUNASPIRE_SECRET_KEY,
            algorithm="HS256",
        )

        spire_url = str(settings.LUNASPIRE_URL).strip()
        if not spire_url:
            raise ImproperlyConfigured("LUNASPIRE_URL is empty")
        if not spire_url.startswith(("http://", "https://")):
            spire_url = f"https://{spire_url}"
        return f"{spire_url.rstrip('/')}/cdn/download?token={download_token}"
    except Exception:
        logger.exception(
            "failed to build direct cdn download url for file_id=%s",
            obj.cdn_file_id,
        )
        return None


def _render_distribution_security_check(obj, refresh_url: str | None = None) -> str:
    if not obj.cdn_hash:
        return '<span class="text-gray-500 font-medium">Файл не загружался (только внешняя ссылка).</span>'

    if obj.virustotal_url:
        vt_link = format_html(
            '<a href="{url}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center rounded-md border border-blue-500/40 px-3 py-2 text-sm font-semibold text-blue-600 hover:text-blue-500 hover:border-blue-500/60 transition-colors">Перейти к отчету VirusTotal</a>',
            url=obj.virustotal_url,
        )
        vt_link_text = format_html(
            '<div class="mt-2 break-all text-xs text-gray-500 dark:text-gray-400">{url}</div>',
            url=obj.virustotal_url,
        )
    else:
        vt_link = '<span class="text-red-600 dark:text-red-400 font-bold">Ссылка VirusTotal не указана.</span>'
        vt_link_text = ""

    download_link_id = f"cdn_download_link_{obj.id}"
    download_status_id = f"cdn_download_status_{obj.id}"
    download_action_url = None
    if refresh_url:
        download_action_url = refresh_url.replace(
            "/refresh-cdn-download/",
            "/direct-cdn-download/",
        )
    direct_cdn_url = _build_direct_cdn_download_url(obj)
    if direct_cdn_url:
        refresh_button = ""
        refresh_script = ""
        if refresh_url and obj.cdn_file_id:
            refresh_button = f"""
                <button type="button" onclick="refreshCdnDownloadLink_{obj.id}()"
                        class="rounded border border-gray-300 dark:border-base-700 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 transition-colors hover:bg-gray-100 dark:hover:bg-base-800 cursor-pointer">
                    Обновить ссылку
                </button>
            """
            refresh_script = f"""
                <script>
                    async function fetchFreshCdnDownloadLink_{obj.id}() {{
                        const linkElem = document.getElementById("{download_link_id}");
                        const statusElem = document.getElementById("{download_status_id}");
                        if (!linkElem || !statusElem) {{
                            return null;
                        }}

                        statusElem.textContent = "Обновление...";
                        linkElem.classList.add("pointer-events-none", "opacity-70");
                        try {{
                            const refreshUrl = "{refresh_url}" + "?_ts=" + Date.now();
                            const response = await fetch(refreshUrl, {{
                                cache: "no-store",
                                credentials: "same-origin",
                                headers: {{
                                    "X-Requested-With": "XMLHttpRequest",
                                }},
                            }});
                            const data = await response.json();
                            if (!response.ok) {{
                                throw new Error(data.error || "Не удалось обновить ссылку");
                            }}

                            linkElem.href = data.url;
                            statusElem.textContent = "Ссылка обновлена";
                            window.setTimeout(function () {{
                                statusElem.textContent = "";
                            }}, 3000);
                            return data.url;
                        }} catch (error) {{
                            statusElem.textContent = error.message || "Ошибка обновления ссылки";
                            return null;
                        }} finally {{
                            linkElem.classList.remove("pointer-events-none", "opacity-70");
                        }}
                    }}

                    async function refreshCdnDownloadLink_{obj.id}() {{
                        await fetchFreshCdnDownloadLink_{obj.id}();
                    }}

                    async function downloadWithFreshLink_{obj.id}(event) {{
                        event.preventDefault();
                        window.open(event.currentTarget.href, "_blank", "noopener,noreferrer");
                    }}
                </script>
            """

        download_link = format_html(
            """
            <div class="flex flex-wrap items-center gap-2">
                <a id="{link_id}" href="{download_href}" target="_blank" rel="noopener noreferrer" onclick="downloadWithFreshLink_{obj_id}(event)"
                   class="inline-flex items-center rounded-md border border-emerald-500/40 px-3 py-2 text-sm font-semibold text-emerald-600 hover:text-emerald-500 hover:border-emerald-500/60 transition-colors">
                    Скачать файл напрямую
                </a>
                {refresh_button}
                <span id="{status_id}" class="text-xs text-gray-500 dark:text-gray-400"></span>
            </div>
            {refresh_script}
            """,
            link_id=download_link_id,
            download_href=download_action_url or direct_cdn_url,
            obj_id=obj.id,
            refresh_button=mark_safe(refresh_button),
            status_id=download_status_id,
            refresh_script=mark_safe(refresh_script),
        )
    elif obj.cdn_file_id:
        download_link = '<span class="text-red-600 dark:text-red-400 text-sm">Не удалось сформировать прямую ссылку на CDN.</span>'
    else:
        download_link = '<span class="text-gray-500 dark:text-gray-400 text-sm">Файл в CDN не найден.</span>'

    input_id = f"vt_input_{obj.id}"
    result_id = f"vt_result_{obj.id}"

    html = f"""
    <div class="p-4 rounded-lg border border-gray-200 dark:border-base-700 bg-gray-50 dark:bg-base-900/60">
        <div class="mb-3">
            <span class="text-sm text-gray-600 dark:text-gray-300">Хэш на CDN (LunaSpire):</span><br>
            <code class="mt-1 inline-block break-all rounded border border-gray-200 dark:border-base-700 bg-white dark:bg-base-900 px-2 py-1 text-xs text-gray-800 dark:text-gray-100">{obj.cdn_hash}</code>
        </div>

        <div class="mb-4 space-y-2">
            {vt_link}
            {vt_link_text}
            <div>{download_link}</div>
        </div>

        <div class="mt-4 rounded-md border border-gray-200 dark:border-base-700 bg-white dark:bg-base-900 p-4">
            <label class="mb-2 block text-sm font-medium text-gray-700 dark:text-gray-200">Вставьте хэш с VirusTotal для сверки:</label>
            <div class="flex flex-wrap items-center gap-2">
                <input
                    type="text"
                    id="{input_id}"
                    class="w-full max-w-lg rounded border border-gray-300 dark:border-base-700 bg-white dark:bg-base-950 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-400 dark:placeholder-gray-500 focus:border-primary-600 focus:ring-primary-600 outline-none"
                    placeholder="Вставьте хэш сюда..."
                >
                <button type="button" onclick="compareHashes_{obj.id}()"
                        class="rounded bg-primary-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-primary-700 cursor-pointer">
                    Сверить
                </button>
            </div>
            <div id="{result_id}" class="mt-3 text-sm min-h-5"></div>
        </div>

        <script>
            function compareHashes_{obj.id}() {{
                const cdnHash = "{obj.cdn_hash}".toLowerCase().trim();
                const inputElem = document.getElementById("{input_id}");
                const resultElem = document.getElementById("{result_id}");
                const vtHash = inputElem.value.toLowerCase().trim();

                if (!vtHash) {{
                    resultElem.innerHTML = "<span class='text-gray-500 dark:text-gray-400'>Пожалуйста, вставьте хэш в поле.</span>";
                    return;
                }}

                if (vtHash === cdnHash) {{
                    resultElem.innerHTML = "<span class='text-green-600 dark:text-green-400 font-bold'>Хэши совпадают! Файл подлинный.</span>";
                }} else {{
                    resultElem.innerHTML = "<span class='text-red-600 dark:text-red-400 font-bold'>Хэши не совпадают! Это другой файл.</span>";
                }}
            }}
        </script>
    </div>
    """
    return html


class DistributionRequestSecurityMixin:
    """Shared admin helpers for distribution request security checks."""

    def get_urls(self):
        urls = super().get_urls()
        info = self.model._meta.app_label, self.model._meta.model_name
        custom_urls = [
            path(
                "<path:object_id>/refresh-cdn-download/",
                self.admin_site.admin_view(self.refresh_cdn_download_link),
                name="%s_%s_refresh_cdn_download" % info,
            ),
            path(
                "<path:object_id>/direct-cdn-download/",
                self.admin_site.admin_view(self.direct_cdn_download),
                name="%s_%s_direct_cdn_download" % info,
            ),
        ]
        return custom_urls + urls

    def refresh_cdn_download_link(self, request, object_id):
        if request.method != "GET":
            return JsonResponse({"error": "Method not allowed"}, status=405)

        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            return JsonResponse({"error": "Заявка не найдена"}, status=404)

        download_url = _build_direct_cdn_download_url(obj)
        if not download_url:
            return JsonResponse(
                {"error": "Не удалось сформировать прямую ссылку на CDN"},
                status=500,
            )

        response = JsonResponse({"url": download_url})
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    def direct_cdn_download(self, request, object_id):
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            return JsonResponse({"error": "Заявка не найдена"}, status=404)

        download_url = _build_direct_cdn_download_url(obj)
        if not download_url:
            return JsonResponse(
                {"error": "Не удалось сформировать прямую ссылку на CDN"},
                status=500,
            )
        return redirect(download_url)

    def _get_security_check_refresh_url(self, obj) -> str:
        info = self.model._meta.app_label, self.model._meta.model_name
        return reverse(
            "admin:%s_%s_refresh_cdn_download" % info,
            args=[obj.pk],
        )

    @admin.display(description="Проверка хэша")
    def security_check(self, obj):
        refresh_url = self._get_security_check_refresh_url(obj)
        return mark_safe(_render_distribution_security_check(obj, refresh_url))

    @admin.display(description="Инструкция для LunaBox")
    def lunabox_manifest_display(self, obj):
        if not obj.lunabox_manifest_is_filled:
            return "-"
        labels = dict(LUNABOX_TYPE_CHOICES)
        label = labels.get(obj.lunabox_type, obj.lunabox_type)
        path = obj.lunabox_path
        if path:
            return f"{label} — путь: {path}"
        return label


class DistributionInlineForm(forms.ModelForm):
    dist_file = forms.FileField(
        label="Файл дистрибуции (CDN)",
        required=False,
    )
    cdn_confirm_token = forms.CharField(
        widget=forms.HiddenInput(), required=False)
    lunabox_type = forms.ChoiceField(
        choices=LUNABOX_TYPE_CHOICES,
        required=True,
        initial="file",
        label=_("PAGE_DIST_FORM_LUNABOX_TYPE_TITLE"))
    lunabox_path = forms.CharField(
        max_length=255,
        required=False,
        label=_("PAGE_DIST_FORM_LUNABOX_PATH_TITLE"),
        widget=forms.TextInput(
            attrs={"placeholder": "folder1/inst.exe"}))

    class Meta:
        model = Distribution
        fields = "__all__"
        widgets = get_translated_widgets_dict({
            "changelog": UnfoldMarkdownTextareaWidget(attrs={"rows": 3}),
        })
        widgets["cdn_file_id"] = forms.HiddenInput()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["lunabox_type"].initial = self.instance.lunabox_type
            self.fields["lunabox_path"].initial = self.instance.lunabox_path

    def clean(self):
        from apps.core.mixins import CDNTokenValidationMixin
        mixin = CDNTokenValidationMixin()
        cleaned_data = super().clean()
        cdn_token = cleaned_data.get("cdn_confirm_token")
        if cdn_token:
            decoded = mixin.validate_cdn_token(cdn_token)
            cleaned_data["cdn_file_id"] = decoded.get("file_id")
            self.instance.cdn_file_id = decoded.get("file_id")

        lunabox_type = cleaned_data.get("lunabox_type")
        lunabox_path = cleaned_data.get("lunabox_path") or ""
        if lunabox_type in LUNABOX_PATH_REQUIRED_TYPES:
            if not lunabox_path.strip():
                self.add_error("lunabox_path", _("PAGE_DIST_FORM_LUNABOX_PATH_REQUIRED"))
        else:
            lunabox_path = ""
        cleaned_data["lunabox_manifest"] = build_lunabox_manifest(
            lunabox_type or "file", lunabox_path)
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.lunabox_manifest = self.cleaned_data.get("lunabox_manifest")
        if "cdn_file_id" in self.cleaned_data and self.cleaned_data["cdn_file_id"]:
            instance.cdn_file_id = self.cleaned_data["cdn_file_id"]
        if commit:
            instance.save()
        return instance


class DistributionInline(TranslationStackedInline):
    model = Distribution
    form = DistributionInlineForm
    fields = (
        "version",
        "dist_file",
        "cdn_confirm_token",
        "cdn_file_id",
        "url",
        "changelog",
        "lunabox_type",
        "lunabox_path",
        "published")
    readonly_fields = ("published",)
    extra = 0

    class Media:
        js = ("js/admin_inline_tabs.js",)


@admin.register(Distribution)
class DistributionAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):
    form = DistributionAdminForm
    list_display = ("app", "version", "published", "download_preview")
    list_filter = SafeDeleteAdmin.list_filter + ["app"]
    readonly_fields = ("published", "download_preview")
    ordering = ["-published"]

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "app",
                    "version",
                    "dist_file",
                    "cdn_confirm_token",
                    "cdn_file_id",
                    "url",
                    "changelog",
                    "lunabox_type",
                    "lunabox_path",
                    "published",
                )
            },
        ),
    )

    class Media:
        # markdown toolbar for changelog + cdn uploader + inline tabs
        js = (
            "js/markdown_client.js",
            "js/markdown_editor.js",
            "js/marketplace_cdn.js",
            "js/admin_inline_tabs.js",
        )

    def render_change_form(
            self,
            request,
            context,
            add=False,
            change=False,
            form_url='',
            obj=None):
        import json
        context.update({"cdn_config": json.dumps({"uploadUrl": f"{getattr(request,
                                                                          'geo_domains',
                                                                          {}).get('SPIRE_URL',
                                                                                  settings.LUNASPIRE_URL)}/cdn/upload",
                                                  "tokenUrl": "/method/user/getPublicUploadToken/",
                                                  "privTokenUrl": "/method/user/getPrivateUploadToken/",
                                                  "appId": obj.app_id if obj else None,
                                                  }),
                        "luna_i18n": json.dumps({"uploading": "Загрузка в LunaSpire...",
                                                 "error": "Ошибка: ",
                                                 "retry": "Повторить",
                                                 "tokenError": "Ошибка токена",
                                                 "fileError": "Ошибка файла: ",
                                                 }),
                        })
        return super().render_change_form(request, context, add=add,
                                          change=change, form_url=form_url, obj=obj)

    def save_model(self, request, obj, form, change):
        file_id = form.cleaned_data.get("cdn_file_id")
        if file_id:
            obj.cdn_file_id = file_id
        super().save_model(request, obj, form, change)

    @admin.display(description="Ссылка")
    def download_preview(self, obj):
        if obj.cdn_file_id:
            return format_html(
                '<a href="/get_dist_file/{}" target="_blank">файл</a>',
                obj.cdn_file_id)
        if obj.url:
            return format_html(
                '<a href="{url}" target="_blank">{url}</a>',
                url=obj.url)
        return "-"


@admin.register(DistributionCreateRequests)
class DistributionCreateAdmin(
        DistributionRequestSecurityMixin,
        SafeDeleteAdmin,
        TabbedTranslationAdmin):
    change_list_template = "admin/decline_forms/change_list_custom.html"
    change_form_template = "admin/decline_forms/change_form_custom.html"

    list_display = ("id", "app", "version", "user", "status", "created_at")
    search_fields = ["app__title", "version", "user__username"]
    list_filter = SafeDeleteAdmin.list_filter + ["status", "created_at"]
    actions_detail = ["approve_request", "reject_request"]
    actions = ["approve_request", "reject_request"]

    readonly_fields = (
        "app",
        "user",
        "status",
        "version",
        "cdn_file_id",
        "url",
        "changelog",
        "lunabox_manifest_display",
        "security_check")

    fieldsets = (
        ("Информация о дистрибуции", {
            "fields": ("app", "version", "changelog", "url", "cdn_file_id", "lunabox_manifest_display")
        }),
        ("Безопасность", {
            "fields": ("security_check",),
            "description": "Сверка хэша от CDN (LunaSpire) с результатами VirusTotal. Если ссылка и хэш не указаны, проверка не выполняется."
        }),
        ("Статус и автор", {
            "fields": ("user", "status")
        }),
    )

    @action(description="Одобрить заявку", icon="check_circle",
            attrs={"class": "bg-success-600 text-white"})
    def approve_request(self, request, object_id):
        req = self.get_object(request, object_id)
        if req.status == "approved":
            self.message_user(request, "Уже одобрено", messages.WARNING)
            return redirect(
                reverse("admin:marketplace_distributioncreaterequests_changelist"))

        dist = approve_dist_create_request(req, actor=request.user)
        if dist is None:
            self.message_user(
                request,
                "Не удалось одобрить заявку",
                messages.ERROR)
            return redirect(
                reverse("admin:marketplace_distributioncreaterequests_changelist"))

        self.message_user(
            request,
            "Дистрибуция успешно создана и опубликована!",
            messages.SUCCESS)
        return redirect(
            reverse("admin:marketplace_distributioncreaterequests_changelist"))

    @action(description="Отклонить заявку", icon="cancel",
            attrs={"class": "bg-error-600 text-white"})
    def reject_request(self, request, object_id):
        req = self.get_object(request, object_id)
        reason = request.POST.get("reject_reason", "Нарушение правил площадки")
        req.status = "rejected"
        req.save()

        send_notification.enqueue(
            user_id=req.user.id,
            title_key="NOTIF_DISTREQ_DECLINED_TITLE",
            content_key="NOTIF_DISTREQ_DECLINED_DESCRIPTION",
            context={
                "app_name": req.app.title,
                "version": req.version,
                "reason": reason},
            meta={
                "icon": "help.png"})
        req.delete(force_policy=HARD_DELETE)
        self.message_user(
            request,
            "Заявка на дистрибуцию отклонена",
            messages.INFO)
        return redirect(
            reverse("admin:marketplace_distributioncreaterequests_changelist"))


@admin.register(DistributionEditRequests)
class DistributionEditRequestAdmin(
        DistributionRequestSecurityMixin,
        SafeDeleteAdmin,
        TabbedTranslationAdmin):
    change_list_template = "admin/decline_forms/change_list_custom.html"
    change_form_template = "admin/decline_forms/change_form_custom.html"

    list_display = (
        "id",
        "target_distribution",
        "user",
        "status",
        "created_at")
    search_fields = (
        "target_distribution__app__title",
        "version",
        "user__username")
    list_filter = SafeDeleteAdmin.list_filter + ["status", "created_at"]
    actions_detail = ["approve_request", "reject_request"]
    actions = ["approve_request", "reject_request"]

    readonly_fields = (
        "app",
        "target_distribution",
        "user",
        "status",
        "version",
        "cdn_file_id",
        "url",
        "changelog",
        "lunabox_manifest_display",
        "security_check")

    fieldsets = (
        ("Информация об изменениях", {
            "fields": ("target_distribution", "version", "changelog", "url", "cdn_file_id", "lunabox_manifest_display")
        }),
        ("Безопасность", {
            "fields": ("security_check",),
            "description": "Сверка хэша от CDN (LunaSpire) с результатами VirusTotal. Если ссылка и хэш не указаны, проверка не выполняется."
        }),
        ("Статус и автор", {
            "fields": ("user", "status")
        }),
    )

    @action(description="Одобрить изменения", icon="check_circle",
            attrs={"class": "bg-success-600 text-white"})
    def approve_request(self, request, object_id):
        req = self.get_object(request, object_id)
        if req.status == "approved":
            self.message_user(request, "Уже одобрено", messages.WARNING)
            return redirect(
                reverse("admin:marketplace_distributioneditrequests_changelist"))

        dist = approve_dist_edit_request(req, actor=request.user)
        if dist is None:
            self.message_user(
                request,
                "Не удалось применить изменения: целевая дистрибуция не найдена",
                messages.ERROR)
            return redirect(
                reverse("admin:marketplace_distributioneditrequests_changelist"))

        self.message_user(
            request,
            "Изменения успешно применены к дистрибуции!",
            messages.SUCCESS)
        return redirect(
            reverse("admin:marketplace_distributioneditrequests_changelist"))

    @action(description="Отклонить изменения", icon="cancel",
            attrs={"class": "bg-error-600 text-white"})
    def reject_request(self, request, object_id):
        req = self.get_object(request, object_id)
        reason = request.POST.get("reject_reason", "Нарушение правил площадки")
        req.status = "rejected"
        req.save()

        send_notification.enqueue(
            user_id=req.user.id,
            title_key="NOTIF_DISTEDITREQ_DECLINED_TITLE",
            content_key="NOTIF_DISTEDITREQ_DECLINED_DESCRIPTION",
            context={
                "app_name": req.app.title,
                "version": req.version,
                "reason": reason},
            meta={
                "icon": "help.png"})
        req.delete(force_policy=HARD_DELETE)
        self.message_user(request, "Правки отклонены", messages.INFO)
        return redirect(
            reverse("admin:marketplace_distributioneditrequests_changelist"))


@admin.register(Category)
class CategoryAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):
    list_display = ("name", "icon", "is_admin_only", "banner_filename")
    search_fields = ("name",)


@admin.register(HomeCategoryBlock)
class HomeCategoryBlockAdmin(unfold_admin.ModelAdmin):
    list_display = ("category", "sort_order", "is_enabled", "apps_limit")
    list_editable = ("sort_order", "is_enabled", "apps_limit")
    list_filter = ("is_enabled",)
    autocomplete_fields = ("category",)
    ordering = ("sort_order", "id")


class BadgeAdminForm(forms.ModelForm):
    class Meta:
        model = Badge
        fields = "__all__"
        widgets = get_translated_widgets_dict({
            'bg_color': UnfoldAdminColorInputWidget(),
            'text_color': UnfoldAdminColorInputWidget(),
            'border_color': UnfoldAdminColorInputWidget(),
            'icon_class': UnfoldAdminTextInputWidget(),
            'icon_text': UnfoldAdminTextInputWidget(),
        })


@admin.register(Badge)
class BadgeAdmin(unfold_admin.ModelAdmin, TabbedTranslationAdmin):
    form = BadgeAdminForm
    list_display = (
        "name",
        "predefined_style",
        "bg_color",
        "text_color",
        "border_color")
    list_filter = ("predefined_style",)
    search_fields = ("name",)
    fieldsets = (
        (None, {
            "fields": ("name", "predefined_style")
        }),
        ("Кастомный стиль (если выбран Выше)", {
            "fields": ("icon_class", "icon_text", "bg_color", "text_color", "border_color"),
            "description": "Эти поля будут учитываться только если Готовый стиль установлен в 'Кастомный'."
        }),
    )


@admin.register(Application)
class ApplicationAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):

    form = ApplicationAdminForm
    inlines = (DistributionInline,)
    readonly_fields = ("display_screenshots",)

    class Meta:
        model = Application

    # exclude = ["user"]

    fieldsets = (
        (
            "Основная информация",
            {
                "fields": (
                    "title",
                    "user",
                    "categories",
                    "badges",
                    "description",
                    "original_author",
                    "slogan",
                    "requirements",
                    "icon_file",
                    "cdn_icon_path",
                    "icon_path",
                    "developer_site",
                    "price",
                    "is_demo",
                    "is_under_dmca",
                    "is_private",
                    "allow_community_distributions"
                )
            },
        ),
        (
            "Управление скриншотами",
            {
                "fields": (
                    "screenshots_files",
                    "cdn_screenshots_data",
                    "screenshots",
                    "display_screenshots",
                ),
            },
        ),
    )

    class Media:
        # markdown toolbar from shared textarea widget + cdn uploader
        js = (
            "js/markdown_client.js",
            "js/markdown_editor.js",
            "js/marketplace_cdn.js",
        )

    def render_change_form(
            self,
            request,
            context,
            add=False,
            change=False,
            form_url='',
            obj=None):
        import json
        context.update({"cdn_config": json.dumps({"uploadUrl": f"{getattr(request,
                                                                          'geo_domains',
                                                                          {}).get('SPIRE_URL',
                                                                                  settings.LUNASPIRE_URL)}/cdn/upload",
                                                  "tokenUrl": "/method/user/getPublicUploadToken/",
                                                  "privTokenUrl": "/method/user/getPrivateUploadToken/",
                                                  "appId": obj.id if obj else None,
                                                  }),
                        "luna_i18n": json.dumps({"uploading": "Загрузка в LunaSpire...",
                                                 "error": "Ошибка: ",
                                                 "retry": "Повторить",
                                                 "tokenError": "Ошибка токена",
                                                 "fileError": "Ошибка файла: ",
                                                 }),
                        })
        return super().render_change_form(request, context, add=add,
                                          change=change, form_url=form_url, obj=obj)

    def save_model(self, request, obj, form, change):
        if not obj.user_id:
            obj.user = request.user
        super().save_model(request, obj, form, change)

    def display_screenshots(self, obj):
        html = ""
        if obj.screenshots:
            for url in obj.screenshot_urls:
                html += f'<img src="{url}" height="90" style="margin-right: 10px; border-radius: 4px; border: 1px solid #ccc; object-fit: cover;" />'

        return mark_safe(html or "Нет скриншотов")

    display_screenshots.short_description = "Предпросмотр"

    @admin.display(description="Категории")
    def display_categories(self, obj):
        return ", ".join([c.name for c in obj.categories.all()])

    @admin.display(description="Бейджи")
    def display_badges(self, obj):
        return ", ".join([b.name for b in obj.badges.all()])

    list_display = [
        "title",
        "user",
        "display_categories",
        "display_badges",
        "is_demo",
        "is_under_dmca",
        "price"]
    list_editable = ["is_demo", "is_under_dmca"]
    list_filter = SafeDeleteAdmin.list_filter + ["is_demo", "is_under_dmca"]
    search_fields = ["title", "user__username"]


@admin.register(AppCreateRequests)
class AppCreateRequestsAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):
    change_list_template = "admin/decline_forms/change_list_custom.html"
    change_form_template = "admin/decline_forms/change_form_custom.html"

    list_display = ("id", "title", "user", "status")
    search_fields = ["title", "id"]
    list_filter = SafeDeleteAdmin.list_filter + [
        "id",
        "status",
    ]
    actions = ["approve_request", "reject_request"]
    actions_detail = ["approve_request", "reject_request"]
    readonly_fields = (
        "user_info_link",
        "user",
        "status",
        "categories",
        "badges",
        "title",
        "description",
        "slogan",
        "original_author",
        "icon_preview",
        "price",
        "is_demo",
        "is_private",
        "allow_community_distributions",
        "screenshots",
        "developer_site",
    )

    def user_info_link(self, obj):
        if not obj.user:
            return "No user associated"

        url_site = f"/profile.php?id={obj.user.id}"

        return format_html(
            """
            <div style="line-height: 1.5;">
                <a href="{}" target="_blank" style="font-weight: bold;">🔗 Профиль на сайте</a><br>
                <br>
                <span style="color: #666;">Email:</span> {}<br>
                <span style="color: #666;">Юзернейм:</span> {}<br>
                <span style="color: #666;">Дата регистрации:</span> {}
            </div>
            """,
            url_site,
            obj.user.email,
            obj.user.username,
            obj.user.date_joined.strftime("%d.%m.%Y %H:%M"),
        )

    def icon_preview(self, obj):
        if obj.icon_path:
            return format_html(
                '<a href="{0}" target="_blank"><img src="{0}" width="50" height="50" style="object-fit: cover; border-radius: 4px; border: 1px solid #ccc;" /></a>',
                obj.icon_url,
            )
        return "Нет иконки"

    user_info_link.short_description = ""

    @action(description="Одобрить заявку",
            icon="check_circle",
            url_path="approve-request")
    def approve_request(self, request, object_id):
        req = self.get_object(request, object_id)

        if req.status == "approved":
            self.message_user(
                request,
                "Эта заявка уже одобрена",
                messages.WARNING)
            return redirect(
                reverse(
                    "admin:marketplace_appcreaterequests_change",
                    args=[object_id]))

        app = approve_app_create_request(req, actor=request.user)
        if app is None:
            self.message_user(
                request,
                "Не удалось одобрить заявку",
                messages.ERROR)
            return redirect(
                reverse(
                    "admin:marketplace_appcreaterequests_change",
                    args=[object_id]))

        self.message_user(
            request,
            "Приложение(-ия) успешно создано!",
            messages.SUCCESS)
        return redirect(
            reverse(
                "admin:marketplace_appcreaterequests_change",
                args=[object_id]))

    @action(
        description="Отклонить заявку",
        icon="cancel",
        attrs={"class": "bg-error-600 text-white"},
    )
    def reject_request(self, request, object_id):
        req = self.get_object(request, object_id)
        reason = request.POST.get("reject_reason")
        req.status = "rejected"
        req.save()
        send_notification.enqueue(
            user_id=req.user.id,
            title_key="NOTIF_APPREQ_DECLINED_TITLE",
            content_key="NOTIF_APPREQ_DECLINED_DESCRIPTION",
            context={"app_name": req.title, "reason": reason},
            meta={"icon": "help.png"}
        )
        req.delete()
        self.message_user(request, "Заявка отклонена", messages.INFO)
        return redirect(
            reverse(
                "admin:marketplace_appcreaterequests_change",
                args=[object_id]))

    fieldsets = (("Основная информация по заявке",
                  {"fields": ("user",
                              "categories",
                              "badges",
                              "title",
                              "slogan",
                              "description",
                              "price",
                              "is_demo",
                              "is_private",
                              "allow_community_distributions",
                              "icon_path",
                              "screenshots",
                              "developer_site",
                              ),
                   "description": "Информация, предоставленная пользователем в заявке на создание приложения",
                   },
                  ),
                 ("Информация по объекту User (пользователю)",
                  {"fields": ("user_info_link",
                              ),
                   "description": "Информация о пользователе, подавшем заявку",
                   },
                  ),
                 )


@admin.register(AppEditRequests)
class AppEditRequestsAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):
    change_list_template = "admin/decline_forms/change_list_custom.html"
    change_form_template = "admin/decline_forms/change_form_custom.html"

    list_display = ("id", "title", "user", "status")
    search_fields = ["title", "id"]
    tabs = [
        ("info_tab", "Основная информация"),
        ("user_tab", "Автор и статус"),
    ]
    list_filter = SafeDeleteAdmin.list_filter + [
        "id",
        "status",
    ]
    actions_detail = ["approve_request", "reject_request"]
    readonly_fields = (
        "user_info_link",
        "user",
        "status",
        "categories",
        "badges",
        "title",
        "original_author",
        "description",
        "requirements",
        "slogan",
        "icon_preview",
        "price",
        "is_demo",
        "is_private",
        "allow_community_distributions",
        "display_screenshots",
        "developer_site",
    )

    def display_screenshots(self, obj):
        html = ""
        if obj.screenshots:
            for url in obj.screenshot_urls:
                html += f'<a href="{url}" target="_blank"><img src="{url}" height="90" style="margin-right: 10px; border-radius: 4px; border: 1px solid #ccc; object-fit: cover;" /></a>'
        return mark_safe(html or "Нет скриншотов")

    display_screenshots.short_description = "Скриншоты"

    def user_info_link(self, obj):
        if not obj.user:
            return "No user associated"
        url_site = f"/profile.php?id={obj.user.id}"
        return format_html(
            """
            <div style="line-height: 1.5;">
                <a href="{}" target="_blank" style="font-weight: bold;">🔗 Профиль на сайте</a><br>
                <br>
                <span style="color: #666;">Email:</span> {}<br>
                <span style="color: #666;">Юзернейм:</span> {}<br>
                <span style="color: #666;">Дата регистрации:</span> {}
            </div>
            """,
            url_site,
            obj.user.email,
            obj.user.username,
            obj.user.date_joined.strftime("%d.%m.%Y %H:%M"),
        )

    def icon_preview(self, obj):
        icon_url = obj.icon_url
        if not obj.icon_path and obj.target_application:
            icon_url = obj.target_application.icon_url

        return format_html(
            '<a href="{0}" target="_blank"><img src="{0}" width="50" height="50" style="object-fit: cover; border-radius: 4px;" /></a>',
            icon_url,
        )

    user_info_link.short_description = ""

    @action(
        description="Одобрить заявку",
        icon="check_circle",
        attrs={"class": "bg-success-600 text-white"},
    )
    def approve_request(self, request, object_id):
        req = self.get_object(request, object_id)

        if req.status == "approved":
            self.message_user(
                request,
                "Заявка уже была одобрена",
                messages.WARNING)
            return redirect(
                reverse(
                    "admin:marketplace_appeditrequests_change",
                    args=[object_id]))

        app = approve_app_edit_request(req, actor=request.user)
        if app is None:
            self.message_user(
                request,
                "Ошибка: Целевое приложение не найдено (возможно, удалено)",
                messages.ERROR,
            )
            return redirect(
                reverse(
                    "admin:marketplace_appeditrequests_change",
                    args=[object_id]))

        self.message_user(
            request,
            "Приложение успешно обновлено!",
            messages.SUCCESS)
        return redirect(
            reverse(
                "admin:marketplace_appeditrequests_change",
                args=[object_id]))

    @action(
        description="Отклонить заявку",
        icon="cancel",
        attrs={"class": "bg-error-600 text-white"},
    )
    def reject_request(self, request, object_id):
        req = self.get_object(request, object_id)
        req.status = "rejected"
        reason = request.POST.get("reject_reason")
        req.save()
        send_notification.enqueue(
            user_id=req.user.id,
            title_key="NOTIF_APPEDITREQ_DECLINED_TITLE",
            content_key="NOTIF_APPEDITREQ_DECLINED_DESCRIPTION",
            context={"app_name": req.title, "reason": reason},
            meta={"icon": "help.png"}
        )
        req.delete()
        self.message_user(request, "Заявка отклонена", messages.INFO)
        return redirect(
            reverse(
                "admin:marketplace_appeditrequests_change",
                args=[object_id]))

    fieldsets = (("Основная информация по заявке",
                  {"fields": ("user",
                              "categories",
                              "title",
                              "slogan",
                              "description",
                              "original_author",
                              "price",
                              "is_demo",
                              "is_private",
                              "allow_community_distributions",
                              "icon_path",
                              "screenshots",
                              "developer_site",
                              ),
                   "description": "Информация, предоставленная пользователем в заявке на изменение информации в приложении",
                   },
                  ),
                 ("Информация по объекту User (пользователю)",
                  {"fields": ("user_info_link",
                              ),
                   "description": "Информация о пользователе, подавшем заявку",
                   },
                  ),
                 )


@admin.register(AppReportRequests)
class AppReportRequestsAdmin(SafeDeleteAdmin):
    list_display = (
        "id",
        "app_link",
        "user_link",
        "get_reason_display",
        "created_at")
    list_filter = SafeDeleteAdmin.list_filter + ["id", "created_at", "reason"]
    actions_detail = ["resolve_report_detail", "dismiss_report_detail"]
    readonly_fields = (
        "user_info_link",
        "app_details_link",
        "user",
        "app",
        "reason",
        "description",
        "created_at",
    )

    @action(description="Пометить как решенное и удалить", icon="done_all")
    def resolve_report_detail(self, request, object_id):
        self._delete_report(request, object_id, "Жалоба обработана и удалена")
        return redirect(
            reverse("admin:marketplace_appreportrequests_changelist"))

    @action(description="Ложная жалоба (удалить)", icon="close")
    def dismiss_report_detail(self, request, object_id):
        self._delete_report(request, object_id, "Запись удалена")
        return redirect(
            reverse("admin:marketplace_appreportrequests_changelist"))

    def _delete_report(self, request, object_id, message):
        obj = AppReportRequests.objects.filter(id=object_id).first()
        if obj:
            obj.delete(force_policy=HARD_DELETE)
            self.message_user(request, message, messages.SUCCESS)

    def _set_status(self, object_id, status_value):
        AppReportRequests.objects.filter(
            id=object_id).update(
            status=status_value)

    def app_link(self, obj):
        if not obj.app:
            return "---"
        url = reverse(
            "admin:marketplace_application_change",
            args=[
                obj.app.id])
        return format_html(
            '<a href="{}" style="font-weight:bold; color: #3b82f6;">{}</a>',
            url,
            obj.app.title,
        )

    app_link.short_description = "Приложение"

    def user_link(self, obj):
        if not obj.user:
            return "---"
        url = reverse("admin:user_user_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = "Автор"

    def user_info_link(self, obj):
        if not obj.user:
            return "Нет данных пользователя"
        url_site = f"/profile.php?id={obj.user.id}"
        return format_html(
            """
            <div style="line-height: 1.5;">
                <a href="{}" target="_blank" style="font-weight: bold; color: #3b82f6;">🔗 Профиль на сайте</a><br>
                <br>
                <span style="color: #666;">Email:</span> {}<br>
                <span style="color: #666;">Юзернейм:</span> {}<br>
                <span style="color: #666;">Дата регистрации:</span> {}
            </div>
            """,
            url_site,
            obj.user.email,
            obj.user.username,
            obj.user.date_joined.strftime("%d.%m.%Y %H:%M"),
        )

    user_info_link.short_description = ""

    def app_details_link(self, obj):
        if not obj.app:
            return "Приложение не найдено"
        icon_url = obj.app.icon_url
        admin_url = reverse(
            "admin:marketplace_application_change",
            args=[
                obj.app.id])

        return format_html(
            """
            <div style="line-height: 1.5;">
                <img src="{}" width="40" height="40" style="border-radius:4px; margin-bottom:10px; object-fit: cover;"/><br>
                <a href="{}" style="font-weight: bold; color: #3b82f6;">Редактировать приложение</a><br>
                <span style="color: #666;">Название:</span> {}<br>
                <span style="color: #666;">Слоган:</span> {}<br>
                <span style="color: #666;">Описание:</span> {}<br>
                <span style="color: #666;">Сайт разработчика:</span> {}<br>
                <span style="color: #666;">Опубликовано:</span> {}<br>
                <span style="color: #666;">ID:</span> {}
            </div>
            """,
            icon_url,
            admin_url,
            obj.app.title,
            obj.app.slogan,
            obj.app.description,
            obj.app.developer_site,
            obj.app.published,
            obj.app.id,
        )

    app_details_link.short_description = ""

    fieldsets = (("Статус и вердикт",
                  {"fields": ("status",
                              ),
                   "description": "Текущее состояние обработки жалобы модератором.",
                   },
                  ),
                 ("Суть жалобы",
                  {"fields": ("reason",
                              "description",
                              "created_at"),
                   "description": "Информация, предоставленная пользователем при подаче репорта.",
                   },
                  ),
                 ("Объект жалобы (Приложение)",
                  {"fields": ("app_details_link",
                              ),
                   "description": "Сведения о приложении, на которое поступила жалоба.",
                   },
                  ),
                 ("Информация об авторе жалобы",
                  {"fields": ("user_info_link",
                              ),
                   "description": "Данные пользователя, отправившего этот репорт.",
                   },
                  ),
                 )


@admin.register(ProblemReportRequests)
class ProblemReportRequestsAdmin(SafeDeleteAdmin):
    list_display = ("id", "user_link", "created_at", "description")
    list_filter = SafeDeleteAdmin.list_filter + ["id", "created_at"]
    actions_detail = ["resolve_report_detail", "dismiss_report_detail"]
    readonly_fields = (
        "user_info_link",
        "user",
        "description",
        "created_at",
    )

    @action(description="Пометить как решенное и удалить", icon="done_all")
    def resolve_report_detail(self, request, object_id):
        self._delete_report(request, object_id, "Жалоба обработана и удалена")
        return redirect(
            reverse("admin:marketplace_problemreportrequests_changelist"))

    @action(description="Ложная жалоба (удалить)", icon="close")
    def dismiss_report_detail(self, request, object_id):
        self._delete_report(request, object_id, "Запись удалена")
        return redirect(
            reverse("admin:marketplace_problemreportrequests_changelist"))

    def _delete_report(self, request, object_id, message):
        obj = ProblemReportRequests.objects.filter(id=object_id).first()
        if obj:
            obj.delete(force_policy=HARD_DELETE)
            self.message_user(request, message, messages.SUCCESS)

    def _set_status(self, object_id, status_value):
        ProblemReportRequests.objects.filter(
            id=object_id).update(
            status=status_value)

    def user_link(self, obj):
        if not obj.user:
            return "---"
        url = reverse("admin:user_user_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)

    user_link.short_description = "Автор"

    def user_info_link(self, obj):
        if not obj.user:
            return "Нет данных пользователя"
        url_site = f"/profile.php?id={obj.user.id}"
        return format_html(
            """
            <div style="line-height: 1.5;">
                <a href="{}" target="_blank" style="font-weight: bold; color: #3b82f6;">🔗 Профиль на сайте</a><br>
                <br>
                <span style="color: #666;">Email:</span> {}<br>
                <span style="color: #666;">Юзернейм:</span> {}<br>
                <span style="color: #666;">Дата регистрации:</span> {}
            </div>
            """,
            url_site,
            obj.user.email,
            obj.user.username,
            obj.user.date_joined.strftime("%d.%m.%Y %H:%M"),
        )

    user_info_link.short_description = ""

    fieldsets = (("Статус и вердикт",
                  {"fields": ("status",
                              ),
                   "description": "Текущее состояние обработки жалобы модератором.",
                   },
                  ),
                 ("Суть жалобы",
                  {"fields": ("description",
                              "created_at"),
                   "description": "Информация, предоставленная пользователем при подаче репорта.",
                   },
                  ),
                 ("Информация об авторе жалобы",
                  {"fields": ("user_info_link",
                              ),
                   "description": "Данные пользователя, отправившего этот репорт.",
                   },
                  ),
                 )


class CollectionItemInline(unfold_admin.TabularInline):
    model = CollectionItem
    extra = 0
    raw_id_fields = ("application",)
    readonly_fields = ("added_at",)


@admin.register(Collection)
class CollectionAdmin(SafeDeleteAdmin, TabbedTranslationAdmin):
    list_display = (
        "id",
        "title",
        "owner",
        "is_system",
        "is_public",
        "updated_at",
    )
    list_filter = SafeDeleteAdmin.list_filter + ["is_system", "is_public"]
    search_fields = ("title", "owner__username")
    raw_id_fields = ("owner",)
    readonly_fields = ("created_at", "updated_at")
    inlines = (CollectionItemInline,)


@admin.register(CollectionFavorite)
class CollectionFavoriteAdmin(unfold_admin.ModelAdmin):
    list_display = ("id", "user", "collection", "created_at")
    search_fields = ("user__username", "collection__title")
    raw_id_fields = ("user", "collection")
    readonly_fields = ("created_at",)


@admin.register(Review)
class ReviewAdmin(unfold_admin.ModelAdmin):
    list_display = (
        "id",
        "app_link",
        "user_link",
        "rating_stars",
        "text_preview",
        "status_badge",
        "ai_score_badge",
        "ai_flags_display",
        "created_at",
    )
    list_filter = (
        "status",
        "rating",
        "created_at",
    )
    search_fields = (
        "text",
        "user__username",
        "application__title",
    )
    raw_id_fields = ("application", "user", "moderated_by", "developer_reply_by")
    readonly_fields = (
        "created_at",
        "updated_at",
        "moderated_at",
        "developer_reply_at",
        "ai_score",
        "ai_flags",
        "ai_reason",
        "ai_raw_response",
    )
    fieldsets = (
        ("Информация об отзыве", {
            "fields": (
                "application",
                "user",
                "rating",
                "text",
                "status",
            ),
        }),
        ("Ответ разработчика", {
            "fields": (
                "developer_reply",
                "developer_reply_by",
                "developer_reply_at",
            ),
        }),
        ("ИИ-модерация (OpenRouter)", {
            "fields": (
                "ai_score",
                "ai_flags",
                "ai_reason",
                "ai_raw_response",
            ),
            "classes": ("collapse",),
        }),
        ("Ручная модерация", {
            "fields": (
                "moderated_by",
                "moderated_at",
                "moderation_note",
            ),
        }),
        ("Временные метки", {
            "fields": (
                "created_at",
                "updated_at",
            ),
            "classes": ("collapse",),
        }),
    )
    list_per_page = 25
    actions = ["approve_reviews", "reject_reviews", "recheck_ai_action"]
    actions_detail = ["approve_detail", "reject_detail"]

    def get_actions_detail(self, request, object_id):
        actions = super().get_actions_detail(request, object_id)
        if not object_id:
            return actions
        review = self.get_object(request, object_id)
        if not review:
            return actions
        if review.status == Review.STATUS_APPROVED:
            return [a for a in actions if getattr(a, "name", "") != "approve_detail"]
        return actions

    def _delete_reviews(self, queryset_or_ids):
        if hasattr(queryset_or_ids, "values_list"):
            app_ids = list(queryset_or_ids.values_list("application_id", flat=True).distinct())
            count, _ = queryset_or_ids.delete()
        else:
            ids = list(queryset_or_ids)
            qs = Review.objects.filter(id__in=ids)
            app_ids = list(qs.values_list("application_id", flat=True).distinct())
            count, _ = qs.delete()
        for app in Application.objects.filter(id__in=app_ids):
            app.update_rating_cache()
        return count

    def _apply_review_status(self, queryset_or_ids, status, user):
        from django.utils import timezone
        if hasattr(queryset_or_ids, "values_list"):
            app_ids = list(queryset_or_ids.values_list("application_id", flat=True).distinct())
            count = queryset_or_ids.update(
                status=status,
                moderated_by=user,
                moderated_at=timezone.now(),
            )
        else:
            ids = list(queryset_or_ids)
            app_ids = list(Review.objects.filter(id__in=ids).values_list("application_id", flat=True).distinct())
            count = Review.objects.filter(id__in=ids).update(
                status=status,
                moderated_by=user,
                moderated_at=timezone.now(),
            )
        for app in Application.objects.filter(id__in=app_ids):
            app.update_rating_cache()
        return count

    def get_changelist_url(self):
        try:
            return reverse("admin:marketplace_review_changelist")
        except Exception:
            return "/admin/marketplace/review/"

    @action(description="Одобрить выбранные отзывы", icon="check_circle")
    def approve_reviews(self, request, queryset):
        count = self._apply_review_status(queryset, Review.STATUS_APPROVED, request.user)
        self.message_user(request, f"Одобрено отзывов: {count}", messages.SUCCESS)

    @action(description="Отклонить выбранные отзывы", icon="cancel")
    def reject_reviews(self, request, queryset):
        count = self._delete_reviews(queryset)
        self.message_user(request, f"Отклонено и удалено отзывов: {count}", messages.WARNING)

    @action(description="Перепроверить через ИИ", icon="refresh")
    def recheck_ai_action(self, request, queryset):
        from .tasks import moderate_review_task
        for review in queryset:
            moderate_review_task.enqueue(review.id)
        self.message_user(request, f"Запущена фоновая перепроверка ИИ для {queryset.count()} отзывов", messages.INFO)

    @action(description="Одобрить отзыв", icon="check")
    def approve_detail(self, request, object_id):
        rev = Review.objects.filter(id=object_id).first()
        changelist_url = self.get_changelist_url()
        if rev and rev.status == Review.STATUS_APPROVED:
            self.message_user(request, "Отзыв уже одобрен", messages.INFO)
            return redirect(changelist_url)
        self._apply_review_status([object_id], Review.STATUS_APPROVED, request.user)
        self.message_user(request, "Отзыв успешно одобрен", messages.SUCCESS)
        return redirect(changelist_url)

    @action(description="Отклонить отзыв", icon="close")
    def reject_detail(self, request, object_id):
        rev = Review.objects.filter(id=object_id).first()
        changelist_url = self.get_changelist_url()
        if not rev:
            self.message_user(request, "Отзыв не найден", messages.INFO)
            return redirect(changelist_url)
        self._delete_reviews([object_id])
        self.message_user(request, "Отзыв отклонён и удалён", messages.WARNING)
        return redirect(changelist_url)

    def app_link(self, obj):
        if not obj.application:
            return "---"
        url = reverse("admin:marketplace_application_change", args=[obj.application.id])
        return format_html(
            '<a href="{}" style="font-weight:bold; color: #3b82f6;">{}</a>',
            url,
            obj.application.title,
        )
    app_link.short_description = "Приложение"

    def user_link(self, obj):
        if not obj.user:
            return "---"
        url = reverse("admin:user_user_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.username)
    user_link.short_description = "Пользователь"

    def rating_stars(self, obj):
        return f"{obj.rating} ★"
    rating_stars.short_description = "Оценка"

    def text_preview(self, obj):
        if not obj.text:
            return mark_safe('<span style="color: #9ca3af;">(без текста)</span>')
        preview = obj.text[:60] + ("..." if len(obj.text) > 60 else "")
        return preview
    text_preview.short_description = "Текст"

    def status_badge(self, obj):
        colors = {
            Review.STATUS_APPROVED: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
            Review.STATUS_PENDING: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
            Review.STATUS_REJECTED: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
        }
        color_class = colors.get(obj.status, "bg-gray-100 text-gray-800")
        return format_html(
            '<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium {}">{}</span>',
            color_class,
            obj.get_status_display(),
        )
    status_badge.short_description = "Статус"

    def ai_score_badge(self, obj):
        if obj.ai_score is None:
            return "---"
        score = obj.ai_score
        if score <= 0.30:
            badge_color = "color: #16a34a; font-weight: bold;"
        elif score >= 0.80:
            badge_color = "color: #dc2626; font-weight: bold;"
        else:
            badge_color = "color: #d97706; font-weight: bold;"
        score_text = f"{score:.2f}"
        return format_html('<span style="{}">{}</span>', badge_color, score_text)
    ai_score_badge.short_description = "AI Скор"

    def ai_flags_display(self, obj):
        if not obj.ai_flags:
            return "---"
        return ", ".join(obj.ai_flags)
    ai_flags_display.short_description = "Флаги"


@admin.register(PendingReview)
class PendingReviewAdmin(ReviewAdmin):
    list_display = (
        "id",
        "app_link",
        "user_link",
        "rating_stars",
        "text_preview",
        "ai_score_badge",
        "ai_flags_display",
        "created_at",
    )
    list_filter = (
        "rating",
        "created_at",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(status=Review.STATUS_PENDING)

    def get_actions_detail(self, request, object_id):
        return unfold_admin.ModelAdmin.get_actions_detail(self, request, object_id)

    def get_changelist_url(self):
        try:
            return reverse("admin:marketplace_pendingreview_changelist")
        except Exception:
            return "/admin/marketplace/pendingreview/"

    def has_module_permission(self, request):
        return request.user.has_perm("marketplace.view_review") or request.user.has_perm(
            "marketplace.view_pendingreview"
        )

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("marketplace.view_review") or request.user.has_perm(
            "marketplace.view_pendingreview"
        )

    def has_change_permission(self, request, obj=None):
        return request.user.has_perm("marketplace.change_review") or request.user.has_perm(
            "marketplace.change_pendingreview"
        )

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm("marketplace.delete_review") or request.user.has_perm(
            "marketplace.delete_pendingreview"
        )

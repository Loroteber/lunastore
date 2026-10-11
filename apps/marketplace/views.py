import jwt
import logging
import os
import time
import urllib.parse
from constance import config
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django_smart_ratelimit import ratelimit
from apps.analytics.services import (
    get_app_analytics,
    track_app_download,
    track_app_rate,
    track_app_view,
)
from apps.core.utils import get_safe_redirect_url, get_client_ip
from apps.core.search import (
    SearchService,
    SearchUnavailableError,
    is_query_too_short,
    normalize_query,
    parse_is_free,
    parse_optional_int,
)
from apps.core.search.pagination import MeilisearchPaginator
from apps.core.search.service import SEARCH_PAGE_SIZE
from apps.user.decorators import developer_required, require_modern_browser
from apps.user.models import User
from .decorators import guard_private_app, user_is_owner
from .forms import (
    AppCreateForm,
    AppEditForm,
    AppReportForm,
    DistributionCreateForm,
    DistributionEditForm,
    ProblemReportForm,
)
from .services.lunabox import notify_unfilled_lunabox_manifests
from .services.moderation import auto_approve_request
from django.db import transaction
from django.db.models import Avg, Count
from .models import (
    AppCreateRequests,
    Application,
    Category,
    Collection,
    CollectionItem,
    Distribution,
    AppEditRequests,
    DistributionCreateRequests,
    DistributionEditRequests,
    Review,
)
from .views_collections import collections
from constance import config
from .services.ai_moderation import moderate_review_text
from .tasks import moderate_review_task

logger = logging.getLogger(__name__)


def _format_legacy_date(value):
    return value.strftime("%d.%m.%Y %H:%M") if value else ""


# redirect to home (index.php) page from (/) page
def home_redirect(request):
    return redirect("/index.php")


# home page
def marketplace(request):
    from apps.marketplace.services.home import (
        HOME_LAYOUT_COMPACT,
        HOME_LAYOUT_RICH,
        build_rich_home_context,
        resolve_home_layout,
    )

    layout = resolve_home_layout(request)
    if layout == HOME_LAYOUT_COMPACT:
        categories = Category.objects.all()
        return render(
            request,
            "index.html",
            {"categories": categories, "home_layout": HOME_LAYOUT_COMPACT},
        )
    context = build_rich_home_context(request)
    return render(request, "index_rich.html", context)


def store_listing(request):
    # store.php?show=top — monthly popular apps listing
    valid_shows = frozenset({"top", "new"})
    valid_views = frozenset({"tiles", "list"})
    show = request.GET.get("show", "top")
    if show not in valid_shows:
        show = "top"
    page = request.GET.get("page")
    view_mode = request.GET.get("view", "tiles")
    if view_mode not in valid_views:
        view_mode = "tiles"

    from apps.analytics.services import get_popular_apps
    from apps.marketplace.services.home import hydrate_apps_by_ids, public_apps_qs

    if show == "top":
        popular = get_popular_apps(days=30, limit=100, event_type="download")
        popular_ids = [item["app_id"] for item in popular]
        apps = hydrate_apps_by_ids(popular_ids)
        if not apps:
            apps = list(public_apps_qs().order_by("-published")[:50])
        title = _("INDEX_MONTHLY_TOP_TITLE")
        description = _("INDEX_MONTHLY_TOP_DESC")
    else:
        apps = list(public_apps_qs().order_by("-published")[:50])
        title = _("INDEX_STORE_LISTING_TITLE")
        description = ""

    paginator = Paginator(apps, 10)
    page_obj = paginator.get_page(page)
    page_range = paginator.get_elided_page_range(
        number=page_obj.number, on_each_side=2, on_ends=1
    )
    context = {
        "page_obj": page_obj,
        "page_range": page_range,
        "name": title,
        "description": description,
        "view_mode": view_mode,
        "count": len(apps),
        "show": show,
        "active_category": None,
    }
    return render(request, "store_listing.html", context)


def category(request):
    valid_views = frozenset({"tiles", "list"})
    id = request.GET.get("id")
    page = request.GET.get("page")
    view_mode = request.GET.get("view", "tiles")
    if view_mode not in valid_views:
        view_mode = "tiles"

    # get model objects
    obj_category = get_object_or_404(Category, id=id)
    obj_apps = Application.objects.select_related("user").prefetch_related(
        "categories", "badges").annotate(
        cached_avg_rating=Avg('reviews__rating')).filter(
            categories=obj_category, is_private=False).order_by("-published")

    # paginator logic
    paginator = Paginator(obj_apps, 10)
    page_obj = paginator.get_page(page)
    page_range = paginator.get_elided_page_range(
        number=page_obj.number, on_each_side=2, on_ends=1
    )

    context = {
        "page_obj": page_obj,
        "page_range": page_range,
        "active_category": obj_category,
        "name": obj_category.name,
        "view_mode": view_mode,
        "description": obj_category.description,
        "count": obj_apps.count,
    }
    return render(request, "category.html", context)


@guard_private_app
def app(request):
    id = request.GET.get("id")
    obj = get_object_or_404(Application.objects.select_related("user"), id=id)
    first_cat_id = obj.categories.values_list("id", flat=True).first()
    track_app_view(request, app_id=obj.pk, category_id=first_cat_id)
    obj_dist = Distribution.objects.filter(
        app__id=id).order_by("-published").first()
    download_page_url = f"{reverse('download')}?id={obj.id}"

    # get all approved reviews for this app
    reviews = Review.objects.filter(
        application=obj,
        status=Review.STATUS_APPROVED,
    ).select_related("user", "developer_reply_by").order_by('-created_at')
    review_count = reviews.count()

    # calculate average rating
    avg_rating = reviews.aggregate(Avg('rating'))['rating__avg']
    avg_rating_display = round(avg_rating, 1) if avg_rating else "0,0"
    if avg_rating:
        avg_rating_display = str(avg_rating_display).replace(".", ",")

    # calculate css class for stars
    star_class = ""
    if avg_rating:
        rounded_val = round(avg_rating * 2) / 2
        star_class = "r" + str(rounded_val).replace(".5",
                                                    "_5").replace(".0", "")

    # get current user rating if logged in
    user_review = None
    if request.user.is_authenticated:
        user_review = Review.objects.filter(
            application=obj, user=request.user
        ).exclude(status=Review.STATUS_REJECTED).first()

    # set up paginator for reviews list
    page = request.GET.get("page", 1)
    paginator = Paginator(reviews, 10)
    page_obj = paginator.get_page(page)
    page_range = paginator.get_elided_page_range(
        number=page_obj.number, on_each_side=2, on_ends=1
    )

    is_liked = False
    likes_count = CollectionItem.objects.filter(
        application=obj, collection__is_system=True
    ).count()
    if request.user.is_authenticated:
        likes_collection = Collection.objects.filter(
            owner=request.user, is_system=True
        ).first()
        if likes_collection is not None:
            is_liked = CollectionItem.objects.filter(
                collection=likes_collection, application=obj
            ).exists()

    context = {
        "app_id": obj.id,
        "is_demo": obj.is_demo,
        "is_dmca": obj.is_under_dmca,
        "is_under_dmca": obj.is_under_dmca,
        "price": obj.price,
        "original_author": obj.original_author,
        "developer_site": obj.developer_site,
        "allow_reviews": obj.allow_reviews,
        "developer_id": obj.user.id,
        "download_page_url": download_page_url,
        "is_translated_to_current_lang": obj.is_translated_to_current_lang,
        "latest_distribution": obj_dist,
        "badges": obj.badges.all(),
        "icon_url": obj.icon_url,
        "title": obj.title,
        "slogan": obj.slogan,
        "description": obj.description,
        "screenshot_urls": obj.screenshot_urls,
        "developer_name": obj.user.username,
        "icon_path": obj.icon_path,
        "requirements": obj.requirements,
        "review_count": review_count,
        "avg_rating_display": avg_rating_display,
        "star_class": star_class,
        "user_review": user_review,
        "page_obj": page_obj,
        "page_range": page_range,
        "collection_saves_count": CollectionItem.objects.filter(application=obj).count(),
        "likes_count": likes_count,
        "is_liked": is_liked,
        "is_app_page": True,
        "is_app_developer": request.user.is_authenticated and (
            request.user == obj.user
            or request.user.is_staff
            or request.user.has_perm("marketplace.change_review")
        ),
    }
    return render(request, "storepage.html", context)


@guard_private_app
def download_list(request):
    app_id = request.GET.get("id")
    if not app_id:
        return redirect("home")

    app_obj = get_object_or_404(Application, id=app_id)
    sort_field = request.GET.get("sort", "version")
    order = request.GET.get("order", "asc")

    valid_fields = {"version": "version", "published": "published"}
    db_sort_field = valid_fields.get(sort_field, "version")

    sort_prefix = "-" if order == "desc" else ""

    distributions = Distribution.objects.filter(app=app_obj).order_by(
        f"{sort_prefix}{db_sort_field}"
    )

    latest_dist = (
        Distribution.objects.filter(
            app=app_obj).order_by(
            "-published",
            "-id").first())
    latest_id = latest_dist.id if latest_dist else None

    dist_rows = []
    for dist in distributions:
        dist_rows.append(
            {
                "id": dist.id,
                "version": dist.version,
                "changelog": dist.changelog,
                "published": _format_legacy_date(dist.published),
                "is_latest": dist.id == latest_id,
                "link": dist.link,
                "has_download": dist.has_download,
                "is_external": dist.is_external,
            }
        )

    page_num = request.GET.get("page", 1)
    paginator = Paginator(dist_rows, 10)

    try:
        page_obj = paginator.page(page_num)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    page_range = page_obj.paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1
    )

    sort_links = {}
    for field in ("version", "published"):
        next_order = "desc" if sort_field == field and order == "asc" else "asc"
        sort_links[field] = (
            f"{reverse('download')}?id={app_obj.id}&sort={field}&order={next_order}"
        )

    dist_rows = []
    for dist in distributions:
        dist_rows.append(
            {
                "id": dist.id,
                "version": dist.version,
                "changelog": dist.changelog,
                "published": _format_legacy_date(dist.published),
                "is_latest": dist.id == latest_id,
                "link": dist.link,
                "has_download": dist.has_download,
            }
        )

    context = {
        "app": app_obj,
        "app_id": app_obj.id,
        "developer_id": app_obj.user.id,
        "is_download_page": True,
        # pass proxy flag to template
        "is_proxy_enabled": getattr(config, 'ENABLE_DISTRIBUTION_PROXY', False),
        "icon_url": app_obj.icon_url,
        "is_demo": app_obj.is_demo,
        "is_under_dmca": app_obj.is_under_dmca,
        "slogan": app_obj.slogan,
        "description": app_obj.description,
        "developer_site": app_obj.developer_site,
        "distributions": page_obj,
        "page_range": page_range,
        "page_obj": page_obj,
        "manage_url": f"{reverse('manage_distributions')}?id={app_obj.id}",
        "current_sort": sort_field,
        "current_order": order,
        "sort_links": sort_links,
        "owner_can_manage": request.user.is_authenticated
        and request.user == app_obj.user,
        "app_link": f"/app.php?id={app_obj.id}",
    }
    return render(request, "download_list.html", context)


@developer_required
@require_modern_browser
@ratelimit(key='ip', rate='10/5m', block=True)
def app_add(request):
    if request.method == "POST":
        form = AppCreateForm(request.POST, request.FILES)
        if form.is_valid():
            app_request = form.save(commit=False)
            app_request.user = request.user
            app_request.save()
            form.save_m2m()

            if auto_approve_request(app_request):
                messages.success(request, _("MSG_TRUSTED_AUTO_APPROVED_APP"))
            else:
                messages.success(request, _("PAGE_ADDAPP_SUCCESS"))
            return redirect("home")
        else:
            # print only system/global errors (e.g. if CDN token is invalid)
            for error in form.non_field_errors():
                messages.error(request, error)

            other_errors = {
                k: v for k,
                v in form.errors.items() if k not in [
                    '__all__',
                    'captcha']}

            if other_errors:
                messages.error(request, _("ERROR_CHECK_FORM"))
    else:
        form = AppCreateForm()

    return render(
        request,
        "app_add.html",
        {
            "form": form,
            # this will go to JS for file upload handling (cdn_upload_url &
            # token_upload_url)
            "cdn_upload_url": f"{getattr(request, 'geo_domains', {}).get('SPIRE_URL', settings.LUNASPIRE_URL)}/cdn/upload",
            "token_upload_url": "/method/user/getPublicUploadToken/",
        },
    )


@login_required
def settings_apps(request):
    if getattr(request, 'limited', False):
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        return redirect(request.META.get('HTTP_REFERER', '/'))

    managed_apps = Application.objects.filter(user=request.user)
    app_requests = AppCreateRequests.objects.filter(user=request.user)
    total_app_requests = app_requests.count() + managed_apps.count()
    return render(
        request,
        "settings_apps.html",
        {
            "managed_apps": managed_apps,
            "app_requests": app_requests,
            "total_app_requests": total_app_requests,
        },
    )


@login_required
@require_modern_browser
@user_is_owner(Application)
@ratelimit(key='ip', rate='20/3m', block=True)
def application_edit_info(request, pk):
    if getattr(request, 'limited', False):
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        return redirect(request.META.get('HTTP_REFERER', '/'))

    obj = get_object_or_404(Application, pk=pk)

    if request.method == "POST":
        form = AppEditForm(
            target_app=obj,
            data=request.POST,
            files=request.FILES)

        if form.is_valid():
            # Apply allow_reviews immediately
            if 'allow_reviews' in form.cleaned_data:
                obj.allow_reviews = form.cleaned_data['allow_reviews']
                obj.save(update_fields=['allow_reviews'])

            edit_request = form.save(commit=False)
            edit_request.user = request.user
            edit_request.save()
            form.save_m2m()

            if auto_approve_request(edit_request):
                messages.success(
                    request, _("MSG_TRUSTED_AUTO_APPROVED_APP_EDIT"))
            else:
                messages.success(request, _("PAGE_ADMIN_APP_MSG_SAVE_SUCCESS"))
            request.session.save()
            return redirect("edit_app_info", pk=obj.pk)
        else:
            messages.error(request, _("PAGE_ADMIN_APP_MSG_SAVE_ERROR"))
            request.session.save()
    else:
        form = AppEditForm(target_app=obj)

    return render(request,
                  "admin_app.html",
                  {"obj": obj,
                   "form": form,
                   "is_edit_page": True,
                   "app_id": obj.pk,
                   "developer_id": obj.user.pk,
                   "developer_site": obj.developer_site,
                   "cdn_upload_url": f"{getattr(request,
                                                'geo_domains',
                                                {}).get('SPIRE_URL',
                                                        settings.LUNASPIRE_URL)}/cdn/upload",
                   "cdn_token_url": "/method/user/getPublicUploadToken/",
                   },
                  )


@login_required
@require_modern_browser
@user_is_owner(Application)
@ratelimit(key='ip', rate='30/1m', block=True)
def application_stats(request, pk):
    if getattr(request, 'limited', False):
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        return redirect(request.META.get('HTTP_REFERER', '/'))

    obj = get_object_or_404(Application, pk=pk)
    stats = get_app_analytics(app_id=obj.pk, days=30, chart_days=14)

    return render(
        request,
        "admin_app_stats.html",
        {
            "obj": obj,
            "app": obj,
            "stats": stats,
            "is_stats_page": True,
            "app_id": obj.pk,
            "developer_id": obj.user.pk,
            "developer_site": obj.developer_site,
        },
    )


def _search_suggest_response(request):
    query = normalize_query(request.GET.get("q"))
    if is_query_too_short(query):
        return JsonResponse({"apps": [], "users": []})

    try:
        limit = int(request.GET.get("limit", "8"))
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 20))

    search_type = request.GET.get("type", "all")
    if search_type not in ("all", "apps", "users"):
        search_type = "all"

    try:
        data = SearchService.suggest(query, limit=limit, search_type=search_type)
    except SearchUnavailableError:
        return JsonResponse({"apps": [], "users": [], "error": "unavailable"}, status=503)

    return JsonResponse(data)


@ratelimit(key='ip', rate='30/1m', block=True)
def search(request):
    if request.GET.get("mode") == "suggest":
        return _search_suggest_response(request)

    query = normalize_query(request.GET.get("q"))
    valid_views = frozenset({"tiles", "list"})
    view_mode = request.GET.get("view", "tiles")
    if view_mode not in valid_views:
        view_mode = "tiles"
    search_type = request.GET.get("type", "apps")
    if search_type not in ("apps", "users"):
        search_type = "apps"

    f_author = parse_optional_int(request.GET.get("author"))
    is_free = parse_is_free(request.GET.get("is_free"))
    f_category = parse_optional_int(request.GET.get("category"))

    categories = Category.objects.all()
    search_unavailable = False

    try:
        page_number = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page_number = 1
    offset = (page_number - 1) * SEARCH_PAGE_SIZE

    if search_type == "users":
        results_qs = User.objects.filter(is_active=True)
        meili_total = None
        if query:
            try:
                user_ids, meili_total = SearchService.search_user_ids(
                    query,
                    limit=SEARCH_PAGE_SIZE,
                    offset=offset,
                )
                results_qs = SearchService.order_queryset_by_ids(results_qs, user_ids)
            except SearchUnavailableError:
                search_unavailable = True
                results_qs = results_qs.none()
                meili_total = 0
        else:
            results_qs = results_qs.order_by("-id")
            paginator = Paginator(results_qs, SEARCH_PAGE_SIZE)
            page_obj = paginator.get_page(page_number)
    else:
        results_qs = Application.objects.select_related("user").prefetch_related(
            "categories", "badges").annotate(
            cached_avg_rating=Avg('reviews__rating')).filter(
            is_private=False,
            is_under_dmca=False,
        )

        if f_category is not None:
            results_qs = results_qs.filter(categories__id=f_category)

        meili_total = None
        if query:
            try:
                app_ids, meili_total = SearchService.search_application_ids(
                    query,
                    limit=SEARCH_PAGE_SIZE,
                    offset=offset,
                    category_id=f_category,
                    author_id=f_author,
                    is_free=is_free,
                )
                results_qs = SearchService.order_queryset_by_ids(results_qs, app_ids)
            except SearchUnavailableError:
                search_unavailable = True
                results_qs = results_qs.none()
                meili_total = 0
        else:
            if f_author is not None:
                results_qs = results_qs.filter(user_id=f_author)
            if is_free:
                results_qs = results_qs.filter(price=0)
            results_qs = results_qs.order_by("-id")
            paginator = Paginator(results_qs, SEARCH_PAGE_SIZE)
            page_obj = paginator.get_page(page_number)

    if query and meili_total is not None:
        paginator = MeilisearchPaginator(results_qs, SEARCH_PAGE_SIZE, meili_total)
        page_obj = paginator.get_page(page_number)

    query_params = request.GET.copy()
    if "page" in query_params:
        del query_params["page"]
    url_params = query_params.urlencode()

    query_params_no_view = query_params.copy()
    if "view" in query_params_no_view:
        del query_params_no_view["view"]
    url_params_no_view = query_params_no_view.urlencode()

    query_params_tabs = query_params_no_view.copy()
    if "type" in query_params_tabs:
        del query_params_tabs["type"]
    url_params_tabs = query_params_tabs.urlencode()

    context = {
        "results": page_obj,
        "query": query,
        "view_mode": view_mode,
        "search_type": search_type,
        "f_author": f_author if f_author is not None else "",
        "search_unavailable": search_unavailable,
        "url_params": url_params,
        "url_params_no_view": url_params_no_view,
        "url_params_tabs": url_params_tabs,
        "categories": categories,
    }
    return render(request, "search.html", context)


@ratelimit(key='ip', rate='20/1m', block=True)
@login_required
def report_app(request):
    id = request.GET.get("id")
    obj = get_object_or_404(Application, id=id)

    if request.method == "POST":
        form = AppReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.user = request.user
            report.app = obj
            report.save()
            messages.success(request, _("PAGE_REPORTAPP_SUCCESS_MSG"))
            return redirect("home")
    else:
        form = AppReportForm()
    context = {
        "form": form,
        "app_id": id,
        "name": obj.title,
        "developer_site": obj.developer_site,
        "developer_id": obj.user.id,
        "is_report_page": True,
        "slogan": obj.slogan,
        "icon": obj.icon_url,
    }
    return render(request, "report_app.html", context)


@ratelimit(key='ip', rate='20/1m', block=True)
@login_required
def report_problem(request):
    if request.method == "POST":
        form = ProblemReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.user = request.user
            report.save()
            messages.success(request, _("PAGE_REPORTPROBLEM_SUCCESS_MSG"))
            return redirect("home")
    else:
        form = ProblemReportForm()
    context = {
        "form": form,
        "is_report_page": True,
    }
    return render(request, "report_problem.html", context)

@developer_required
@require_modern_browser
@ratelimit(key='ip', rate='20/1m', block=True)
def distribution_create(request, app_id):
    app_obj = get_object_or_404(Application, id=app_id)

    if app_obj.user != request.user:
        if not app_obj.allow_community_distributions:
            raise PermissionDenied("COMMUNITY_DISTRIBUTIONS_ARE_NOT_ALLOWED")
    else:
        return redirect(reverse("manage_distributions") +
                        "?id=" + str(app_obj.id))

    form = DistributionCreateForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        distribution_request = form.save(commit=False)
        distribution_request.app = app_obj
        distribution_request.user = request.user
        distribution_request.status = "pending"
        distribution_request.save()

        messages.success(request, _("PAGE_MANAGEDIST_CREATE_SUCCESS"))
        return redirect(reverse("download") +
                        "?id=" + str(app_obj.id))

    elif request.method == "POST" and not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)
                
    return render(
        request,
        "distribution_form.html",
        {
            "form": form,
            "app": app_obj,
            "app_id": app_obj.id,
            "is_edit_page": False,
        },
    )

@login_required
@require_modern_browser
@ratelimit(key='ip', rate='20/1m', block=True)
def manage_distributions(request):
    app_id = request.GET.get("id")
    app_obj = get_object_or_404(Application, id=app_id)

    if app_obj.user != request.user:
        raise PermissionDenied("ERROR_YOURE_NOT_OWNER_OF_APP")

    distributions = Distribution.objects.filter(
        app=app_obj).order_by("-published")

    pending_requests = DistributionCreateRequests.objects.filter(
        app=app_obj, status="pending").order_by("-created_at")
    pending_edits = DistributionEditRequests.objects.filter(
        target_distribution__app=app_obj,
        status="pending").order_by("-created_at")

    form = DistributionCreateForm(request.POST or None, request.FILES or None)

    if request.method == "POST" and form.is_valid():
        distribution = form.save(commit=False)
        distribution.app = app_obj
        distribution.user = request.user
        distribution.save()

        if auto_approve_request(distribution):
            messages.success(request, _("MSG_TRUSTED_AUTO_APPROVED_DIST"))
        else:
            messages.success(request, _("PAGE_MANAGEDIST_CREATE_SUCCESS"))
        return redirect(reverse("manage_distributions") +
                        "?id=" + str(app_obj.id))
    elif request.method == "POST" and not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)

    # remind developers to fill in the lunabox manifest for old releases
    notify_unfilled_lunabox_manifests(request.user)

    dist_rows = []
    for dist in distributions:
        dist_rows.append({"id": dist.id,
                          "version": dist.version,
                          "published": _format_legacy_date(dist.published),
                          "changelog": dist.changelog,
                          "edit_url": reverse("distribution_edit",
                                              kwargs={"dist_pk": dist.pk}),
                          "delete_url": reverse("distribution_delete",
                                                kwargs={"dist_pk": dist.pk}),
                          })

    page_num = request.GET.get("page", 1)
    paginator = Paginator(dist_rows, 10)

    try:
        page_obj = paginator.page(page_num)
    except (PageNotAnInteger, EmptyPage):
        page_obj = paginator.page(1)

    page_range = page_obj.paginator.get_elided_page_range(
        page_obj.number, on_each_side=1, on_ends=1
    )

    context = {"app": app_obj,
               "form": form,
               "distributions": page_obj,
               "developer_site": app_obj.developer_site,
               "developer_id": app_obj.user.id,
               "app_id": app_obj.id,
               "page_obj": page_obj,
               "is_edit_page": True,
               "page_range": page_range,
               "pending_requests": pending_requests,
               "pending_edits": pending_edits,
               "get_token_url": "/method/user/getPrivateUploadToken/",
               "cdn_upload_url": f"{getattr(request,
                                            'geo_domains',
                                            {}).get('SPIRE_URL',
                                                    settings.LUNASPIRE_URL)}/cdn/upload",
               "download_list_url": reverse("download") + "?id=" + str(app_obj.id),
               }
    return render(request, "manage_distributions.html", context)


@login_required
@require_modern_browser
@ratelimit(key='ip', rate='20/1m', block=True)
def distribution_edit(request, dist_pk):
    distribution = get_object_or_404(Distribution, pk=dist_pk)
    if distribution.app.user != request.user:
        raise PermissionDenied(_("ERROR_YOURE_NOT_OWNER_OF_APP"))

    # collect initial data (including changelog translations)
    initial_data = {
        "version": distribution.version,
        "url": distribution.url,
        "lunabox_type": distribution.lunabox_type,
        "lunabox_path": distribution.lunabox_path,
    }

    # automatically populate changelog translations
    for lang_code, lang_name in settings.LANGUAGES:
        lang_field = f"changelog_{lang_code}"
        short_lang_field = f"changelog_{lang_code.split('-')[0].lower()}"

        if hasattr(distribution, lang_field):
            initial_data[lang_field] = getattr(distribution, lang_field)
        elif hasattr(distribution, short_lang_field):
            initial_data[short_lang_field] = getattr(
                distribution, short_lang_field)

    # initialize form with initial data
    form = DistributionEditForm(
        request.POST or None,
        user=request.user,
        target_dist=distribution,
        initial=initial_data
    )

    if request.method == "POST" and form.is_valid():
        edit_req = form.save(commit=False)
        edit_req.app = distribution.app
        edit_req.save()

        if auto_approve_request(edit_req):
            messages.success(request, _("MSG_TRUSTED_AUTO_APPROVED_DIST_EDIT"))
        else:
            messages.success(request, _("MSG_DIST_EDIT_REQ_SENT"))
        return redirect(
            reverse("manage_distributions") + "?id=" + str(distribution.app.id)
        )
    elif request.method == "POST" and not form.is_valid():
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(request, error)

    context = {
        "form": form,
        "app": distribution.app,
        "distribution": distribution,
        "developer_site": distribution.app.developer_site,
        "developer_id": distribution.app.user.id,
        "app_id": distribution.app.id,
        "is_edit_page": True,
        "get_token_url": "/method/user/getPrivateUploadToken/",
        "cdn_upload_url": f"{
            getattr(
                request,
                'geo_domains',
                {}).get(
                'SPIRE_URL',
                settings.LUNASPIRE_URL)}/cdn/upload",
        "download_list_url": reverse("download") +
        "?id=" +
        str(
            distribution.app.id),
    }
    return render(request, "distribution_form.html", context)


def get_real_ip(group, request):
    return get_client_ip(request)


@login_required
@ratelimit(key='user', rate='60/1h', block=False)
@ratelimit(key=get_real_ip, rate='120/1h', block=False)
def rate_app(request):
    if getattr(request, 'limited', False):
        app_id = request.GET.get("id") or request.POST.get("id")
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        if app_id:
            return redirect(f"{reverse('app')}?id={app_id}")
        return redirect("home")

    # save user rating here
    if request.method == "POST":
        app_id = request.GET.get("id") or request.POST.get("id")
        rating = request.POST.get("rating")

        # redirect if missing data
        if not app_id or not rating:
            return redirect("home")

        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                raise ValueError
        except ValueError:
            return redirect(f"{reverse('app')}?id={app_id}")

        # get the app object
        obj = get_object_or_404(Application, id=app_id)

        if not obj.allow_reviews:
            messages.error(request, _("PAGE_APP_RATING_DISABLED"))
            return redirect(f"{reverse('app')}?id={app_id}")

        review_text = (request.POST.get("text") or "").strip()[:2000]
        status = Review.STATUS_APPROVED
        ai_score = None
        ai_flags = []
        ai_reason = ""
        ai_raw_response = None
        should_enqueue_async = False

        if review_text:
            ai_enabled = getattr(config, "AI_MODERATION_ENABLED", True)
            fast_path = getattr(config, "AI_MODERATION_FAST_PATH", True)

            if ai_enabled and fast_path:
                result = moderate_review_text(review_text, app_title=obj.title)
                if result.reason == "service_error" or (result.decision == "pending" and result.score is None):
                    status = Review.STATUS_PENDING
                    ai_reason = result.reason or "service_error"
                    if result.reason == "service_error":
                        should_enqueue_async = True
                    messages.info(request, _("PAGE_APP_REVIEW_PENDING"))
                elif result.decision == Review.STATUS_REJECTED:
                    logger.info(
                        "Review for app %s by user %s rejected by AI: flags=%s, score=%s, reason=%s",
                        obj.id, request.user.id, result.flags, result.score, result.reason,
                    )
                    messages.error(request, _("PAGE_APP_REVIEW_REJECTED_GENERIC"))
                    return redirect(f"{reverse('app')}?id={app_id}")
                elif result.decision == Review.STATUS_APPROVED:
                    status = Review.STATUS_APPROVED
                    ai_score = result.score
                    ai_flags = result.flags
                    ai_reason = result.reason
                    ai_raw_response = result.raw_response
                    messages.success(request, _("PAGE_APP_REVIEW_APPROVED"))
                else:  # pending (borderline score 0.31 - 0.79)
                    status = Review.STATUS_PENDING
                    ai_score = result.score
                    ai_flags = result.flags
                    ai_reason = result.reason
                    ai_raw_response = result.raw_response
                    messages.info(request, _("PAGE_APP_REVIEW_PENDING"))

                    # Alert moderators about borderline review in Telegram
                    from apps.core.tasks import send_telegram_notification
                    from django.utils.html import escape
                    clean_preview = escape(review_text[:300])
                    score_str = f"{result.score:.2f}" if result.score is not None else "N/A"
                    flags_str = ", ".join(result.flags) if result.flags else "нет"
                    tg_msg = (
                        "📝 <b>Новый отзыв требует проверки модератором</b>\n\n"
                        f"Приложение: <b>{escape(obj.title)}</b>\n"
                        f"Автор: <b>{escape(request.user.username)}</b> (Оценка: {rating} ★)\n"
                        f"AI Скор: <code>{score_str}</code> (Флаги: {escape(flags_str)})\n"
                        f"Причина: <i>{escape(result.reason or 'ручная очередь')}</i>\n\n"
                        f"Текст:\n<blockquote>{clean_preview}</blockquote>"
                    )
                    try:
                        send_telegram_notification(tg_msg)
                    except Exception as exc:
                        logger.warning("Failed to send telegram notification for review: %s", exc)
            elif ai_enabled:
                status = Review.STATUS_PENDING
                should_enqueue_async = True
                messages.info(request, _("PAGE_APP_REVIEW_PENDING"))
            else:
                status = Review.STATUS_PENDING
                ai_reason = "ai_disabled"
                messages.info(request, _("PAGE_APP_REVIEW_PENDING"))
        else:
            messages.success(request, _("PAGE_APP_RATING_SUCCESS"))

        # create or update the review
        review, created = Review.objects.update_or_create(
            application=obj,
            user=request.user,
            defaults={
                'rating': rating,
                'text': review_text,
                'status': status,
                'ai_score': ai_score,
                'ai_flags': ai_flags,
                'ai_reason': ai_reason,
                'ai_raw_response': ai_raw_response,
            }
        )

        if should_enqueue_async:
            moderate_review_task.enqueue(review.id)

        track_app_rate(request, app_id=obj.pk, rating=rating)

        # go back to app page
        return redirect(f"{reverse('app')}?id={app_id}")
    return redirect("home")


@login_required
@require_POST
@ratelimit(key='user', rate='60/1h', block=False)
def delete_review(request):
    if getattr(request, 'limited', False):
        review_id = request.POST.get("id")
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        if review_id:
            review = Review.objects.filter(id=review_id).first()
            if review:
                return redirect(f"{reverse('app')}?id={review.application.id}")
        return redirect("home")

    review_id = request.POST.get("id")
    if not review_id:
        return redirect("home")

    review = get_object_or_404(Review, id=review_id)
    if review.user != request.user and not request.user.has_perm(
            "marketplace.delete_review"):
        messages.error(request, _("PAGE_APP_RATING_DELETE_DENIED"))
        return redirect(f"{reverse('app')}?id={review.application.id}")

    app = review.application
    app_id = app.id
    review.delete()
    app.update_rating_cache()
    messages.success(request, _("PAGE_APP_RATING_DELETE_SUCCESS"))

    next_url = get_safe_redirect_url(
        request,
        request.POST.get("next"),
        fallback=f"{reverse('app')}?id={app_id}",
    )
    return redirect(next_url)


@login_required
@require_POST
@ratelimit(key='user', rate='30/1h', block=False)
def reply_review(request):
    if getattr(request, 'limited', False):
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        review_id = request.POST.get("review_id") or request.GET.get("id")
        if review_id:
            review = Review.objects.filter(id=review_id).select_related("application").first()
            if review:
                return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")
        return redirect("home")

    review_id = request.POST.get("review_id") or request.GET.get("id")
    if not review_id:
        return redirect("home")

    review = get_object_or_404(
        Review.objects.select_related("application", "application__user"),
        id=review_id,
    )

    if review.status != Review.STATUS_APPROVED:
        return HttpResponseForbidden(_("PAGE_APP_REVIEW_REPLY_DENIED"))

    is_dev = (
        request.user == review.application.user
        or request.user.is_staff
        or request.user.has_perm("marketplace.change_review")
    )
    if not is_dev:
        return HttpResponseForbidden(_("PAGE_APP_REVIEW_REPLY_DENIED"))

    reply_text = (request.POST.get("developer_reply") or "").strip()[:2000]
    if not reply_text:
        messages.error(request, _("PAGE_APP_REVIEW_REPLY_EMPTY"))
        return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")

    ai_enabled = getattr(config, "AI_MODERATION_ENABLED", True)
    if ai_enabled:
        mod_result = moderate_review_text(reply_text, app_title=review.application.title)
        if mod_result.decision == Review.STATUS_REJECTED:
            messages.error(request, _("PAGE_APP_REVIEW_REJECTED_GENERIC"))
            return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")

    review.developer_reply = reply_text
    review.developer_reply_at = timezone.now()
    review.developer_reply_by = request.user
    review.save(update_fields=["developer_reply", "developer_reply_at", "developer_reply_by", "updated_at"])

    messages.success(request, _("PAGE_APP_REVIEW_REPLY_SUCCESS"))
    return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")


@login_required
@require_POST
@ratelimit(key='user', rate='30/1h', block=False)
def delete_review_reply(request):
    if getattr(request, 'limited', False):
        messages.error(request, _("ERROR_RATE_LIMIT_EXCEEDED"))
        review_id = request.POST.get("review_id") or request.GET.get("id")
        if review_id:
            review = Review.objects.filter(id=review_id).select_related("application").first()
            if review:
                return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")
        return redirect("home")

    review_id = request.POST.get("review_id") or request.GET.get("id")
    if not review_id:
        return redirect("home")

    review = get_object_or_404(
        Review.objects.select_related("application", "application__user"),
        id=review_id,
    )

    is_dev = (
        request.user == review.application.user
        or request.user == review.developer_reply_by
        or request.user.is_staff
        or request.user.has_perm("marketplace.delete_review")
    )
    if not is_dev:
        return HttpResponseForbidden(_("PAGE_APP_REVIEW_REPLY_DELETE_DENIED"))

    review.developer_reply = ""
    review.developer_reply_at = None
    review.developer_reply_by = None
    review.save(update_fields=["developer_reply", "developer_reply_at", "developer_reply_by", "updated_at"])

    messages.success(request, _("PAGE_APP_REVIEW_REPLY_DELETED"))
    return redirect(f"{reverse('app')}?id={review.application.id}#rev{review.id}")


@login_required
@require_modern_browser
def distribution_delete(request, dist_pk):
    distribution = get_object_or_404(Distribution, pk=dist_pk)
    if distribution.app.user != request.user:
        raise PermissionDenied(_("ERROR_YOURE_NOT_OWNER_OF_APP"))

    if request.method == "POST":
        distribution.delete()
        messages.success(request, _("Дистрибуция удалена"))
    else:
        messages.warning(request, _("Нужно подтвердить удаление через POST"))

    return redirect(reverse("manage_distributions") +
                    "?id=" + str(distribution.app.id))


def distribution_list_page(request, app_id):
    app_obj = get_object_or_404(Application, id=app_id)
    distributions = app_obj.distributions.filter(
        deleted__isnull=True).order_by("-published")
    return render(
        request,
        "marketplace/download_list.html",
        {"app": app_obj, "distributions": distributions},
    )


def get_file_action(request, dist_pk):
    dist = get_object_or_404(Distribution.objects.select_related("app"), pk=dist_pk)
    app = dist.app

    # block private app downloads for non-owners
    if app.is_private:
        if not request.user.is_authenticated or app.user_id != request.user.id:
            raise PermissionDenied(_("ERROR_YOURE_NOT_OWNER_OF_APP"))

    # track download analytics (first category for CH category filters)
    first_cat_id = app.categories.values_list("id", flat=True).first()
    track_app_download(
        request,
        app_id=app.pk,
        distribution_id=dist.pk,
        category_id=first_cat_id,
    )

    if dist.cdn_file_id:
        payload = {
            "type": "cdn-download",
            "file_id": int(dist.cdn_file_id),
            "exp": int(time.time()) + 600,
            "app_id": int(app.id),
        }
        if request.user.is_authenticated:
            payload["user_id"] = int(request.user.id)
        try:
            download_token = jwt.encode(
                payload, settings.LUNASPIRE_SECRET_KEY, algorithm="HS256"
            )
        except Exception:
            logger.exception("failed to encode cdn download jwt")
            raise Http404("File not found")

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        is_retro = any(
            sig in user_agent for sig in [
                "MSIE 5",
                "MSIE 6",
                "MSIE 7",
                "MSIE 8"])

        protocol = "http" if is_retro else "https"

        spire_url = getattr(
            request, 'geo_domains', {}).get(
            'SPIRE_URL', settings.LUNASPIRE_URL)
        domain = spire_url.replace('https://', '').replace('http://', '')
        cdn_url = f"{protocol}://{domain}/cdn/download?token={download_token}"
        return redirect(cdn_url)

    if dist.url:
        is_proxy_requested = request.GET.get('proxy') == '1'
        # proxy through nginx if enabled
        if is_proxy_requested and getattr(
                config, 'ENABLE_DISTRIBUTION_PROXY', False):
            parsed_url = urllib.parse.urlparse(dist.url)
            path = parsed_url.path
            _, ext = os.path.splitext(path)
            if not ext and parsed_url.fragment:
                _, ext = os.path.splitext(parsed_url.fragment)

            # sanitize names
            app_name = "".join([c for c in dist.app.title if c.isalnum() or c in (
                " ", "-", "_")]).strip().replace(" ", "_")
            version = "".join([c for c in dist.version if c.isalnum() or c in (
                " ", "-", ".", "_")]).strip().replace(" ", "_")

            # build filename
            if ext:
                if version:
                    filename = f"{app_name}_{version}{ext}"
                else:
                    filename = f"{app_name}{ext}"
            else:
                # fallback to basename
                basename = os.path.basename(path)
                filename = basename if basename else f"{app_name}.download"

            # set proxy headers
            response = HttpResponse()
            response['X-Accel-Redirect'] = f'/_px/{dist.url}'
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            return response

        # fallback to direct download
        return redirect(dist.url)

    raise Http404("File not found")

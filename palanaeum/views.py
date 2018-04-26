import time
from collections import defaultdict
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import Count, Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import ugettext_lazy as _

from palanaeum.configuration import get_config
from palanaeum.decorators import json_response
from palanaeum.forms import UserCreationFormWithEmail, UserSettingsForm, \
    EmailChangeForm, SortForm
from palanaeum.models import UserSettings, UsersEntryCollection, Event, \
    AudioSource, Entry, Tag, ImageSource, RelatedSite
from palanaeum.search import init_filters, execute_filters, get_search_results, \
    paginate_search_results
from palanaeum.utils import is_contributor


def index(request):
    """
    Draw the home page.
    """
    page_length = UserSettings.get_page_length(request)
    newest_events = Event.all_visible.exclude(entries=None).prefetch_related('entries', 'tags')[:page_length]
    events_count = Event.all_visible.filter().count()
    entries_count = Entry.all_visible.filter().count()
    sources_count = AudioSource.all_visible.filter().count()

    new_sources = []
    new_sources.extend(AudioSource.all_visible.order_by('-created_date')[:5])
    new_sources.extend(ImageSource.all_visible.order_by('-created_date')[:5])
    new_sources.sort(key=lambda source: source.created_date or
                                        timezone.datetime(1900, 1, 1, tzinfo=timezone.get_current_timezone()),
                     reverse=True)
    new_sources = new_sources[:5]

    related_sites = RelatedSite.objects.all()

    welcome_text = get_config('index_hello')

    return render(request, 'palanaeum/index.html', {'newest_events': newest_events, 'events_count': events_count,
                                                    'entries_count': entries_count, 'sources_count': sources_count,
                                                    'new_sources': new_sources, 'related_sites': related_sites,
                                                    'welcome_text': welcome_text})


def events(request):
    """
    Display a list of events that are present in the system.
    """
    events = Event.all_visible.all()

    sort_form = SortForm((('name', _('name')), ('date', _('date'))), request.GET,
                         initial={'sort_by': 'date', 'sort_ord': '-'})

    if sort_form.is_valid():
        sort_by = sort_form.cleaned_data['sort_by']
        sort_ord = sort_form.cleaned_data['sort_ord']
        events.order_by('{}{}'.format(sort_ord, sort_by), 'name')

    page_length = UserSettings.get_page_length(request)
    paginator = Paginator(events, page_length, orphans=page_length // 10)

    page_num = request.GET.get('page', '1')

    try:
        page = paginator.page(page_num)
    except PageNotAnInteger:
        page = paginator.page(1)
    except EmptyPage:
        page = paginator.page(paginator.num_pages)

    return render(request, 'palanaeum/events_list.html', {'paginator': paginator, 'page': page})


def event_no_slug(request, event_id):
    """
    Redirect to a version with slug in URL.
    """
    event = get_object_or_404(Event, pk=event_id)
    return redirect('view_event', event_id, slugify(event.name))


def view_entry(request, entry_id):
    """
    Redirect user to proper event and entry.
    """
    entry = get_object_or_404(Entry, pk=entry_id)
    return redirect(reverse('view_event', args=(entry.event_id, slugify(entry.event.name))) + '#e{}'.format(entry.id))


def view_event(request, event_id):
    """
    Display single Event page.
    """
    event = get_object_or_404(Event, pk=event_id)

    if not event.visible():
        raise PermissionDenied

    entry_ids = Entry.all_visible.filter(event=event).values_list('id', flat=True)
    entries_map = Entry.prefetch_entries(entry_ids, show_unapproved=is_contributor(request))

    entries = sorted(entries_map.values(), key=lambda e: e.order)
    sources = list(event.sources_iterator())

    approval_msg = get_config('approval_message')
    if event.review_state == Event.REVIEW_APPROVED:
        approval_explanation = get_config('review_reviewed_explanation')
    elif event.review_state == Event.REVIEW_PENDING:
        approval_explanation = get_config('review_pending_explanation')
    else:
        approval_explanation = ''

    return render(request, 'palanaeum/event.html', {'event': event, 'entries': entries, 'sources': sources,
                                                    'approval_msg': approval_msg,
                                                    'review_message': approval_explanation})


def password_reset_complete(request):
    """
    Write a message about success and redirect user to login page.
    """
    messages.success(request, _('Your password has been set. You may go ahead and log in now.'))
    return redirect('auth_login')


def password_change_complete(request):
    """
    Write a message about success and redirect user to login page.
    """
    messages.success(request, _('Your password has been changed.'))
    return redirect('auth_settings')


def register_user(request):
    """
    Display a registration form. If it's a POST request, create a new user.
    """
    if request.method == 'POST':
        form = UserCreationFormWithEmail(request.POST)
        if form.is_valid():
            new_user = form.save(commit=True)
            settings = UserSettings.objects.create(user=new_user)
            settings.page_length = get_config('default_page_length')
            settings.save()
            # We're not gonna play with e-mail confirmation and activation for now.
            messages.success(request, _('Congratulations! You have created a new account. '
                                        'You can sign in using your credentials.'))
            return redirect('auth_login')
    else:
        form = UserCreationFormWithEmail()

    return render(request, 'palanaeum/auth/register.html', {'form': form})


@login_required(login_url='auth_login')
def user_settings(request):
    """
    Display user control panel.
    """
    settings_obj = UserSettings.objects.get(user=request.user)

    if request.method == 'POST':
        email_form = EmailChangeForm(request.POST, user=request.user)
        settings_form = UserSettingsForm(request.POST, instance=settings_obj)
        if settings_form.is_valid() and email_form.is_valid():
            email_form.save()
            settings_obj = settings_form.save()
            request.session['page_length'] = settings_obj.page_length
            messages.success(request, _('Settings have been saved.'))
            return redirect('auth_settings')
    else:
        email_form = EmailChangeForm(initial={'email': request.user.email}, user=request.user)
        settings_form = UserSettingsForm(instance=settings_obj)

    return render(request, 'palanaeum/auth/settings.html', {'settings_form': settings_form, 'email_form': email_form})


def adv_search(request):
    """
    Display an advances search form + search results.
    """
    filters = init_filters(request)

    ordering = request.GET.get('ordering', 'rank')

    search_params = [urlencode({'ordering': ordering})]
    for search_filter in filters:
        if search_filter:
            search_params.append(search_filter.as_url_param())
    search_params = "&".join(search_params)

    if any(filters):
        start_time = time.time()
        entries_scores = execute_filters(filters)

        entries, paginator, page = paginate_search_results(request, get_search_results(entries_scores, ordering))
        search_time = time.time() - start_time
    else:
        entries = []
        paginator = None
        page = None
        search_time = 0

    return render(request, 'palanaeum/search/adv_search_results.html',
                  {'paginator': paginator, 'entries': entries, 'filters': filters, 'search_done': any(filters),
                   'query': request.GET.get('query', ''), 'search_params': search_params,
                   'page': page, 'search_time': search_time, 'ordering': ordering})


@json_response
def get_tags(request):
    """
    Return a tag list suitable for select2.
    """
    query = request.GET.get('q', '')
    tags = Tag.objects.filter(
        Q(name__startswith=query) | Q(name__contains=' ' + query)
    ).annotate(entry_count=Count('entries'), event_count=Count('events'))
    return {'results': [
        {
            'id': t.name,
            'text': "{} ({})".format(t.name, t.entry_count + t.event_count)
        } for t in sorted(tags, key=lambda tag: -(tag.entry_count + tag.event_count))]}


def tags_list(request):
    """
    Show a list of tags that are used in the system.
    """
    event_tags = defaultdict(list)
    entry_tags = defaultdict(list)

    for tag in Tag.objects.exclude(entries=None).annotate(entry_count=Count('entries')):
        entry_tags[tag.entry_count].append(tag)

    for tag in Tag.objects.exclude(events=None).annotate(event_count=Count('events')):
        event_tags[tag.event_count].append(tag)

    entry_tags = sorted(entry_tags.items(), reverse=True)
    event_tags = sorted(event_tags.items(), reverse=True)

    return render(request, 'palanaeum/tags_list.html', {'entry_tags': entry_tags, 'event_tags': event_tags})
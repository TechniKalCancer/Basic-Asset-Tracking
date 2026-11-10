"""Pages: help."""
from flask import flash, jsonify, redirect, render_template, request, url_for
from foxdesk.core import app, db
from foxdesk.models import HELP_ARTICLE_TYPES, HelpArticle, TicketCategory
from foxdesk.services.auth import _log_activity, kiosk_or_login_required, require_permission


@app.route('/help')
@kiosk_or_login_required
def help_page():
    """
    Public-facing FAQ + How-To page — same access level as Check In/Check
    Out/Home (any admin session or enrolled kiosk device), since it's a
    help resource for everyone using the app, not an admin-only feature.
    Only shows active articles; management (including drafts) is at
    /admin/help.
    """
    faqs = HelpArticle.query.filter_by(article_type='faq', is_active=True) \
        .order_by(HelpArticle.sort_order, HelpArticle.title).all()
    howtos = HelpArticle.query.filter_by(article_type='howto', is_active=True) \
        .order_by(HelpArticle.sort_order, HelpArticle.title).all()
    return render_template('help.html', faqs=faqs, howtos=howtos)


@app.route('/help/suggest')
@kiosk_or_login_required
def help_suggest():
    """Help articles that look like they answer what someone is typing on
    Submit a Ticket (subject words, and any pinned to the chosen category)."""
    from foxdesk.services.ticket_forms import suggest_articles
    q = request.args.get('q', '')[:200]
    category_id = request.args.get('category_id', type=int)
    return jsonify([dict(id=a.id, title=a.title, url=f'/help#help-{a.id}') for a in suggest_articles(q, category_id)])


@app.route('/admin/help')
@require_permission('admin')
def admin_help():
    articles = HelpArticle.query.order_by(HelpArticle.article_type, HelpArticle.sort_order, HelpArticle.title).all()
    return render_template('admin_help.html', articles=articles, article_types=HELP_ARTICLE_TYPES)


def _help_article_form_values():
    return {
        'article_type': request.form.get('article_type', 'faq').strip(),
        'title': request.form.get('title', '').strip(),
        'body': request.form.get('body', '').strip(),
        'sort_order': request.form.get('sort_order', type=int) or 0,
        'is_active': bool(request.form.get('is_active')),
        'ticket_category_id': request.form.get('ticket_category_id', type=int) or None,
    }


def _form_page(article, form):
    return render_template('admin_help_form.html', article=article, form=form, article_types=HELP_ARTICLE_TYPES,
                           categories=TicketCategory.query.filter_by(is_active=True).order_by(TicketCategory.name).all())


@app.route('/admin/help/new', methods=['GET', 'POST'])
@require_permission('admin')
def admin_help_new():
    if request.method == 'POST':
        values = _help_article_form_values()
        if values['article_type'] not in HELP_ARTICLE_TYPES:
            values['article_type'] = 'faq'
        if not values['title'] or not values['body']:
            flash('Title and body are both required.', 'error')
            return _form_page(None, values)

        article = HelpArticle(**values)
        db.session.add(article)
        _log_activity('help_article_add', f'Added {HELP_ARTICLE_TYPES[values["article_type"]]} "{values["title"]}".')
        db.session.commit()
        flash(f'Added "{values["title"]}".', 'success')
        return redirect(url_for('admin_help'))

    return _form_page(None, None)


@app.route('/admin/help/<int:article_id>/edit', methods=['GET', 'POST'])
@require_permission('admin')
def admin_help_edit(article_id):
    article = db.get_or_404(HelpArticle, article_id)
    if request.method == 'POST':
        values = _help_article_form_values()
        if values['article_type'] not in HELP_ARTICLE_TYPES:
            values['article_type'] = article.article_type
        if not values['title'] or not values['body']:
            flash('Title and body are both required.', 'error')
            return _form_page(article, values)

        article.article_type = values['article_type']
        article.title = values['title']
        article.body = values['body']
        article.sort_order = values['sort_order']
        article.is_active = values['is_active']
        article.ticket_category_id = values['ticket_category_id']
        _log_activity('help_article_edit', f'Edited {HELP_ARTICLE_TYPES[article.article_type]} "{article.title}".')
        db.session.commit()
        flash(f'Updated "{article.title}".', 'success')
        return redirect(url_for('admin_help'))

    return _form_page(article, None)


@app.route('/admin/help/<int:article_id>/delete', methods=['POST'])
@require_permission('admin')
def admin_help_delete(article_id):
    article = db.get_or_404(HelpArticle, article_id)
    title, article_type = article.title, article.article_type
    db.session.delete(article)
    _log_activity('help_article_delete', f'Deleted {HELP_ARTICLE_TYPES[article_type]} "{title}".')
    db.session.commit()
    flash(f'Deleted "{title}".', 'success')
    return redirect(url_for('admin_help'))

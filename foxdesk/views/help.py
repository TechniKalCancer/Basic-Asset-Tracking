"""Pages: help."""
from flask import flash, redirect, render_template, request, url_for
from foxdesk.core import app, db
from foxdesk.models import HELP_ARTICLE_TYPES, HelpArticle
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
    }


@app.route('/admin/help/new', methods=['GET', 'POST'])
@require_permission('admin')
def admin_help_new():
    if request.method == 'POST':
        values = _help_article_form_values()
        if values['article_type'] not in HELP_ARTICLE_TYPES:
            values['article_type'] = 'faq'
        if not values['title'] or not values['body']:
            flash('Title and body are both required.', 'error')
            return render_template('admin_help_form.html', article=None, form=values, article_types=HELP_ARTICLE_TYPES)

        article = HelpArticle(**values)
        db.session.add(article)
        _log_activity('help_article_add', f'Added {HELP_ARTICLE_TYPES[values["article_type"]]} "{values["title"]}".')
        db.session.commit()
        flash(f'Added "{values["title"]}".', 'success')
        return redirect(url_for('admin_help'))

    return render_template('admin_help_form.html', article=None, form=None, article_types=HELP_ARTICLE_TYPES)


@app.route('/admin/help/<int:article_id>/edit', methods=['GET', 'POST'])
@require_permission('admin')
def admin_help_edit(article_id):
    article = HelpArticle.query.get_or_404(article_id)
    if request.method == 'POST':
        values = _help_article_form_values()
        if values['article_type'] not in HELP_ARTICLE_TYPES:
            values['article_type'] = article.article_type
        if not values['title'] or not values['body']:
            flash('Title and body are both required.', 'error')
            return render_template('admin_help_form.html', article=article, form=values, article_types=HELP_ARTICLE_TYPES)

        article.article_type = values['article_type']
        article.title = values['title']
        article.body = values['body']
        article.sort_order = values['sort_order']
        article.is_active = values['is_active']
        _log_activity('help_article_edit', f'Edited {HELP_ARTICLE_TYPES[article.article_type]} "{article.title}".')
        db.session.commit()
        flash(f'Updated "{article.title}".', 'success')
        return redirect(url_for('admin_help'))

    return render_template('admin_help_form.html', article=article, form=None, article_types=HELP_ARTICLE_TYPES)


@app.route('/admin/help/<int:article_id>/delete', methods=['POST'])
@require_permission('admin')
def admin_help_delete(article_id):
    article = HelpArticle.query.get_or_404(article_id)
    title, article_type = article.title, article.article_type
    db.session.delete(article)
    _log_activity('help_article_delete', f'Deleted {HELP_ARTICLE_TYPES[article_type]} "{title}".')
    db.session.commit()
    flash(f'Deleted "{title}".', 'success')
    return redirect(url_for('admin_help'))

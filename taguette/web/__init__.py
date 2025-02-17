import gettext
import json
import logging
from markupsafe import Markup
import pkg_resources
from tornado.routing import URLSpec
from tornado.web import HTTPError

from .base import Application, BaseHandler
from . import api, export, views


logger = logging.getLogger(__name__)


class RedirectAccount(BaseHandler):
    def get(self):
        if not self.application.config['MULTIUSER']:
            raise HTTPError(404)
        return self.redirect(self.reverse_url('account'), True)


class TranslationJs(BaseHandler):
    def get(self):
        catalog = {}

        d = pkg_resources.resource_filename('taguette', 'l10n')
        gettext_trans = gettext.translation('taguette_javascript', d,
                                            [self.locale.code], fallback=True)
        if gettext_trans is not None:
            if isinstance(gettext_trans, gettext.GNUTranslations):
                catalog = gettext_trans._catalog

        language = Markup(json.dumps(self.locale.code))
        catalog = Markup(json.dumps(catalog))
        self.set_header('Content-Type', 'text/javascript')
        return self.render('trans.js',
                           language=language,
                           catalog=catalog)


class MessagesJs(BaseHandler):
    async def get(self):
        if not self.application.messages_event.is_set():
            await self.application.messages_event.wait()
        messages = json.dumps(self.application.messages)
        self.set_header('Content-Type', 'text/javascript')
        return await self.render('messages.js',
                                 messages=Markup(messages))


class Error404(BaseHandler):
    def initialize(self):
        self.set_status(404)

    def prepare(self):
        self.render(
            'error.html',
            error_title="Error 404",
            error_message="This page does not exist.",
        )
        raise HTTPError(404)


def make_app(config, debug=False, xsrf_cookies=True):
    return Application(
        [
            # Basic pages
            URLSpec('/', views.Index, name='index'),
            URLSpec('/cookies', views.CookiesPrompt, name='cookies_prompt'),
            URLSpec('/login', views.Login, name='login'),
            URLSpec('/logout', views.Logout, name='logout'),
            URLSpec('/register', views.Register, name='register'),
            URLSpec('/tos', views.TermsOfService, name='tos'),
            URLSpec('/account', views.Account, name='account'),
            URLSpec('/reset_password', views.AskResetPassword,
                    name='reset_password'),
            URLSpec('/new_password', views.SetNewPassword,
                    name='new_password'),
            URLSpec('/project/new', views.ProjectAdd, name='new_project'),
            URLSpec('/project/import', views.ProjectImport,
                    name='import_project'),
            URLSpec('/project/([0-9]+)/delete', views.ProjectDelete,
                    name='delete_project'),
            URLSpec('/project/([0-9]+)/import_codebook', views.ImportCodebook,
                    name='import_codebook'),

            # Project view
            URLSpec('/project/([0-9]+)', views.Project, name='project'),
            URLSpec('/project/([0-9]+)/document/[0-9]+', views.Project,
                    name='project_doc'),
            URLSpec('/project/([0-9]+)/highlights/.*', views.Project,
                    name='project_tag'),

            # Export options
            URLSpec('/project/([0-9]+)/export/project\\.sqlite3',
                    export.ExportSqlite, name='export_project_sqlite'),
            URLSpec('/project/([0-9]+)/export/codebook\\.qdc',
                    export.ExportCodebookXml, name='export_codebook_qdc'),
            URLSpec('/project/([0-9]+)/export/codebook\\.csv',
                    export.ExportCodebookCsv, name='export_codebook_csv'),
            URLSpec('/project/([0-9]+)/export/codebook\\.xlsx',
                    export.ExportCodebookXlsx, name='export_codebook_xlsx'),
            URLSpec('/project/([0-9]+)/export/codebook\\.([a-z0-3]{2,4})',
                    export.ExportCodebookDoc, name='export_codebook_doc'),
            URLSpec('/project/([0-9]+)/export/document/'
                    '([^/]+)\\.([a-z0-9]{2,4})',
                    export.ExportDocument, name='export_document'),
            URLSpec('/project/([0-9]+)/export/highlights/(.*)\\.csv',
                    export.ExportHighlightsCsv),
            URLSpec('/project/([0-9]+)/export/highlights/(.*)\\.xlsx',
                    export.ExportHighlightsXlsx),
            URLSpec('/project/([0-9]+)/export/highlights/'
                    '(.*)\\.([a-z0-3]{2,4})',
                    export.ExportHighlightsDoc, name='export_highlights_doc'),

            # API
            URLSpec('/api/check_user', api.CheckUser),
            URLSpec('/api/import', api.ImportProject),
            URLSpec('/api/project/([0-9]+)', api.ProjectMeta),
            URLSpec('/api/project/([0-9]+)/document/new', api.DocumentAdd),
            URLSpec('/api/project/([0-9]+)/document/([0-9]+)',
                    api.Document),
            URLSpec('/api/project/([0-9]+)/document/([0-9]+)/contents',
                    api.DocumentContents),
            URLSpec('/api/project/([0-9]+)/document/([0-9]+)/highlight/new',
                    api.HighlightAdd),
            URLSpec(
                '/api/project/([0-9]+)/document/([0-9]+)/highlight/([0-9]+)',
                api.HighlightUpdate),
            URLSpec('/api/project/([0-9]+)/highlights/(.*)',
                    api.Highlights),
            URLSpec('/api/project/([0-9]+)/tag/new', api.TagAdd),
            URLSpec('/api/project/([0-9]+)/tag/([0-9]+)', api.TagUpdate),
            URLSpec('/api/project/([0-9]+)/tag/merge', api.TagMerge),
            URLSpec('/api/project/([0-9]+)/members', api.MembersUpdate),
            URLSpec('/api/project/([0-9]+)/events', api.ProjectEvents),

            # Translation catalog and functions
            URLSpec('/trans\\.js', TranslationJs, name='trans.js'),

            # Messages
            URLSpec('/messages\\.js', MessagesJs, name='messages.js'),

            # Well-known URLs
            URLSpec('/\\.well-known/change-password', RedirectAccount),
        ],
        static_path=pkg_resources.resource_filename('taguette', 'static'),
        login_url='/login',
        xsrf_cookies=xsrf_cookies,
        debug=debug,
        config=config,
        default_handler_class=Error404,
    )

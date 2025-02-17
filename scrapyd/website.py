import socket
from datetime import datetime, timedelta
from urllib.parse import urlparse

from scrapy.utils.misc import load_object
from twisted.application.service import IServiceCollection
from twisted.web import resource, static

from scrapyd.interfaces import IEggStorage, IPoller, ISpiderScheduler
from scrapyd.jobstorage import job_items_url, job_log_url


class PrefixHeaderMixin:
    def get_base_path(self, txrequest):
        return txrequest.getHeader(self.prefix_header) or ''


class Root(resource.Resource):

    def __init__(self, config, app):
        resource.Resource.__init__(self)
        self.debug = config.getboolean('debug', False)
        self.runner = config.get('runner')
        self.prefix_header = config.get('prefix_header')
        self.logsdir = config.get('logs_dir') or 'logs'
        logsdir_enc = self.logsdir.encode('ascii', 'ignore')
        self.putChild(logsdir_enc, static.File(logsdir_enc, 'text/plain'))
        itemsdir = config.get('items_dir')
        self.local_items = itemsdir and (urlparse(itemsdir).scheme.lower() in ['', 'file'])
        self.app = app
        self.nodename = config.get('node_name', socket.gethostname())
        self.putChild(b'', Home(self, self.local_items))
        if self.local_items:
            self.putChild(b'items', static.File(itemsdir, 'text/plain'))
        self.putChild(b'jobs', Jobs(self, self.local_items))
        services = config.items('services', ())
        for servName, servClsName in services:
            servCls = load_object(servClsName)
            self.putChild(servName.encode('utf-8'), servCls(self))
        self.update_projects()

    def update_projects(self):
        self.poller.update_projects()
        self.scheduler.update_projects()

    @property
    def launcher(self):
        app = IServiceCollection(self.app, self.app)
        return app.getServiceNamed('launcher')

    @property
    def scheduler(self):
        return self.app.getComponent(ISpiderScheduler)

    @property
    def eggstorage(self):
        return self.app.getComponent(IEggStorage)

    @property
    def poller(self):
        return self.app.getComponent(IPoller)


class Home(PrefixHeaderMixin, resource.Resource):

    def __init__(self, root, local_items):
        resource.Resource.__init__(self)
        self.root = root
        self.local_items = local_items
        self.prefix_header = root.prefix_header

    def render_GET(self, txrequest):
        vars = {
            'projects': ', '.join(self.root.scheduler.list_projects()),
            'base_path': self.get_base_path(txrequest),
            'logs_path': self.root.logsdir,
        }
        s = """
<html>
<head><title>Scrapyd</title></head>
<body>
<h1>Scrapyd</h1>
<p>Available projects: <b>%(projects)s</b></p>
<ul>
<li><a href="%(base_path)s/jobs">Jobs</a></li>
"""
        if self.local_items:
            s += '<li><a href="%(base_path)s/items/">Items</a></li>'
        s += """
<li><a href="%(base_path)s/%(logs_path)s/">Logs</a></li>
<li><a href="https://scrapyd.readthedocs.io/en/latest/">Documentation</a></li>
</ul>

<h2>How to schedule a spider?</h2>

<p>To schedule a spider you need to use the API (this web UI is only for
monitoring)</p>

<p>Example using <a href="https://curl.se/">curl</a>:</p>
<p><code>curl http://localhost:6800/schedule.json -d project=default -d spider=somespider</code></p>

<p>For more information about the API, see the
<a href="https://scrapyd.readthedocs.io/en/latest/">Scrapyd documentation</a></p>
</body>
</html>
"""
        txrequest.setHeader('Content-Type', 'text/html; charset=utf-8')
        s = (s % vars).encode('utf8')
        txrequest.setHeader('Content-Length', str(len(s)))
        return s


def microsec_trunc(timelike):
    if hasattr(timelike, 'microsecond'):
        ms = timelike.microsecond
    else:
        ms = timelike.microseconds
    return timelike - timedelta(microseconds=ms)


class Jobs(PrefixHeaderMixin, resource.Resource):

    def __init__(self, root, local_items):
        resource.Resource.__init__(self)
        self.root = root
        self.local_items = local_items
        self.prefix_header = root.prefix_header

    cancel_button = """
    <form method="post" action="{base_path}/cancel.json">
    <input type="hidden" name="project" value="{project}"/>
    <input type="hidden" name="job" value="{jobid}"/>
    <input type="submit" style="float: left;" value="Cancel"/>
    </form>
    """.format

    header_cols = [
        'Project', 'Spider',
        'Job', 'PID',
        'Start', 'Runtime', 'Finish',
        'Log', 'Items',
        'Cancel',
    ]

    def gen_css(self):
        css = [
            '#jobs>thead td {text-align: center; font-weight: bold}',
            '#jobs>tbody>tr:first-child {background-color: #eee}',
        ]
        if not self.local_items:
            col_idx = self.header_cols.index('Items') + 1
            css.append('#jobs>*>tr>*:nth-child(%d) {display: none}' % col_idx)
        if b'cancel.json' not in self.root.children:
            col_idx = self.header_cols.index('Cancel') + 1
            css.append('#jobs>*>tr>*:nth-child(%d) {display: none}' % col_idx)
        return '\n'.join(css)

    def prep_row(self, cells):
        if not isinstance(cells, dict):
            assert len(cells) == len(self.header_cols)
        else:
            cells = [cells.get(k) for k in self.header_cols]
        cells = ['<td>%s</td>' % ('' if c is None else c) for c in cells]
        return '<tr>%s</tr>' % ''.join(cells)

    def prep_doc(self):
        return (
            '<html>'
            '<head>'
            '<title>Scrapyd</title>'
            '<style type="text/css">' + self.gen_css() + '</style>'
            '</head>'
            '<body><h1>Jobs</h1>'
            '<p><a href="./">Go up</a></p>'
            + self.prep_table() +
            '</body>'
            '</html>'
        )

    def prep_table(self):
        return (
            '<table id="jobs" border="1">'
            '<thead>' + self.prep_row(self.header_cols) + '</thead>'
            '<tbody>'
            + '<tr><th colspan="%d">Pending</th></tr>' % len(self.header_cols)
            + self.prep_tab_pending() +
            '</tbody>'
            '<tbody>'
            + '<tr><th colspan="%d">Running</th></tr>' % len(self.header_cols)
            + self.prep_tab_running() +
            '</tbody>'
            '<tbody>'
            + '<tr><th colspan="%d">Finished</th></tr>' % len(self.header_cols)
            + self.prep_tab_finished() +
            '</tbody>'
            '</table>'
        )

    def prep_tab_pending(self):
        return '\n'.join(
            self.prep_row({
                "Project": project,
                "Spider": m['name'],
                "Job": m['_job'],
                "Cancel": self.cancel_button(project=project, jobid=m['_job'], base_path=self.base_path),
            })
            for project, queue in self.root.poller.queues.items()
            for m in queue.list()
        )

    def prep_tab_running(self):
        return '\n'.join(
            self.prep_row({
                "Project": p.project,
                "Spider": p.spider,
                "Job": p.job,
                "PID": p.pid,
                "Start": microsec_trunc(p.start_time),
                "Runtime": microsec_trunc(datetime.now() - p.start_time),
                "Log": f'<a href="/{self.root.logsdir}{self.base_path}{job_log_url(p)}">Log</a>',
                "Items": f'<a href="{self.base_path}{job_items_url(p)}">Items</a>',
                "Cancel": self.cancel_button(project=p.project, jobid=p.job, base_path=self.base_path),
            })
            for p in self.root.launcher.processes.values()
        )

    def prep_tab_finished(self):
        return '\n'.join(
            self.prep_row({
                "Project": p.project,
                "Spider": p.spider,
                "Job": p.job,
                "Start": microsec_trunc(p.start_time),
                "Runtime": microsec_trunc(p.end_time - p.start_time),
                "Finish": microsec_trunc(p.end_time),
                "Log": f'<a href="/{self.root.logsdir}{self.base_path}{job_log_url(p)}">Log</a>',
                "Items": f'<a href="{self.base_path}{job_items_url(p)}">Items</a>',
            })
            for p in self.root.launcher.finished
        )

    def render(self, txrequest):
        self.base_path = self.get_base_path(txrequest)
        doc = self.prep_doc()
        txrequest.setHeader('Content-Type', 'text/html; charset=utf-8')
        doc = doc.encode('utf-8')
        txrequest.setHeader('Content-Length', str(len(doc)))
        return doc

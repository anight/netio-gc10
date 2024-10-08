# Picoweb web pico-framework for Pycopy, https://github.com/pfalcon/pycopy
# Copyright (c) 2014-2020 Paul Sokolovsky
# SPDX-License-Identifier: MIT
import sys
import gc
import asyncio


SEND_BUFSZ = 128


def unquote_plus(s):
    s = s.replace("+", " ")
    arr = s.split("%")
    arr2 = [chr(int(x[:2], 16)) + x[2:] for x in arr[1:]]
    return arr[0] + "".join(arr2)

def parse_qs(s):
    res = {}
    if s:
        pairs = s.split("&")
        for p in pairs:
            vals = [unquote_plus(x) for x in p.split("=", 1)]
            if len(vals) == 1:
                vals.append(True)
            old = res.get(vals[0])
            if old is not None:
                if not isinstance(old, list):
                    old = [old]
                    res[vals[0]] = old
                old.append(vals[1])
            else:
                res[vals[0]] = vals[1]
    return res

def get_mime_type(fname):
    # Provide minimal detection of important file
    # types to keep browsers happy
    if fname.endswith(".html"):
        return "text/html"
    if fname.endswith(".css"):
        return "text/css"
    if fname.endswith(".png") or fname.endswith(".jpg"):
        return "image"
    return "text/plain"

async def sendstream(writer, f):
    buf = bytearray(SEND_BUFSZ)
    while True:
        l = f.readinto(buf)
        if not l:
            break
        await writer.awrite(buf, 0, l)

async def jsonify(writer, dict):
    import ujson
    await start_response(writer, "application/json")
    await writer.awrite(ujson.dumps(dict))

async def start_response(writer, content_type="text/html; charset=utf-8", status="200", headers=None):
    await writer.awrite("HTTP/1.0 %s NA\r\n" % status)
    await writer.awrite("Content-Type: ")
    await writer.awrite(content_type)
    if not headers:
        await writer.awrite("\r\n\r\n")
        return
    await writer.awrite("\r\n")
    if isinstance(headers, bytes) or isinstance(headers, str):
        await writer.awrite(headers)
    else:
        for k, v in headers.items():
            await writer.awrite(k)
            await writer.awrite(": ")
            await writer.awrite(v)
            await writer.awrite("\r\n")
    await writer.awrite("\r\n")

def http_error(writer, status):
    await start_response(writer, status=status)
    await writer.awrite(str(status))
    await writer.awrite("\n")


class HTTPRequest:

    def __init__(self):
        pass

    async def read_form_data(self):
        size = int(self.headers[b"Content-Length"])
        data = await self.reader.readexactly(size)
        form = parse_qs(data.decode())
        self.form = form

    def parse_qs(self):
        form = parse_qs(self.qs)
        self.form = form


class WebApp:

    def __init__(self, pkg, routes=None):
        if routes:
            self.url_map = routes
        else:
            self.url_map = []
        if pkg and pkg != "__main__":
            self.pkg = pkg.split(".", 1)[0]
        else:
            self.pkg = None
        self.mounts = []
        self.inited = False
        # Instantiated lazily
        self.template_loader = None
        self.headers_mode = "parse"

    async def parse_headers(self, reader):
        headers = {}
        while True:
            l = await reader.readline()
            if l == b"\r\n":
                break
            k, v = l.split(b":", 1)
            headers[k] = v.strip()
        return headers

    async def _handle(self, reader, writer):
        close = True
        req = None
        try:
            request_line = await reader.readline()
            if request_line == b"":
                await writer.aclose()
                return
            req = HTTPRequest()
            # TODO: bytes vs str
            request_line = request_line.decode()
            method, path, proto = request_line.split()
            path = path.split("?", 1)
            qs = ""
            if len(path) > 1:
                qs = path[1]
            path = path[0]

            #print("================")
            #print(req, writer)
            #print(req, (method, path, qs, proto), req.headers)

            # Find which mounted subapp (if any) should handle this request
            app = self
            while True:
                found = False
                for subapp in app.mounts:
                    root = subapp.url
                    # print(path, "vs", root)
                    if path[:len(root)] == root:
                        app = subapp
                        found = True
                        path = path[len(root):]
                        if not path.startswith("/"):
                            path = "/" + path
                        break
                if not found:
                    break

            # We initialize apps on demand, when they really get requests
            if not app.inited:
                app.init()

            # Find handler to serve this request in app's url_map
            found = False
            for e in app.url_map:
                pattern = e[0]
                handler = e[1]
                extra = {}
                if len(e) > 2:
                    extra = e[2]

                if path == pattern:
                    found = True
                    break
                elif not isinstance(pattern, str):
                    # Anything which is non-string assumed to be a ducktype
                    # pattern matcher, whose .match() method is called. (Note:
                    # Django uses .search() instead, but .match() is more
                    # efficient and we're not exactly compatible with Django
                    # URL matching anyway.)
                    m = pattern.match(path)
                    if m:
                        req.url_match = m
                        found = True
                        break

            if not found:
                headers_mode = "skip"
            else:
                headers_mode = extra.get("headers", self.headers_mode)

            if headers_mode == "skip":
                while True:
                    l = await reader.readline()
                    if l == b"\r\n":
                        break
            elif headers_mode == "parse":
                req.headers = await self.parse_headers(reader)
            else:
                assert headers_mode == "leave"

            if found:
                req.method = method
                req.path = path
                req.qs = qs
                req.reader = reader
                close = await handler(req, writer)
            else:
                await http_error(writer, status="404")

        except Exception as e:
            sys.print_exception(e)

        if close is not False:
            await writer.aclose()

    def handle_exc(self, req, resp, e):
        # Can be overriden by subclasses. req may be not (fully) initialized.
        # resp may already have (partial) content written.
        # NOTE: It's your responsibility to not throw exceptions out of
        # handle_exc(). If exception is thrown, it will be propagated, and
        # your webapp will terminate.
        # This method is a coroutine.
        return

    def mount(self, url, app):
        "Mount a sub-app at the url of current app."
        # Inspired by Bottle. It might seem that dispatching to
        # subapps would rather be handled by normal routes, but
        # arguably, that's less efficient. Taking into account
        # that paradigmatically there's difference between handing
        # an action and delegating responisibilities to another
        # app, Bottle's way was followed.
        app.url = url
        self.mounts.append(app)
        # TODO: Consider instead to do better subapp prefix matching
        # in _handle() above.
        self.mounts.sort(key=lambda app: len(app.url), reverse=True)

    def route(self, url, **kwargs):
        def _route(f):
            self.url_map.append((url, f, kwargs))
            return f
        return _route

    def add_url_rule(self, url, func, **kwargs):
        # Note: this method skips Flask's "endpoint" argument,
        # because it's alleged bloat.
        self.url_map.append((url, func, kwargs))

    def _load_template(self, tmpl_name):
        if self.template_loader is None:
            import utemplate.source
            self.template_loader = utemplate.source.Loader(self.pkg, "templates")
        return self.template_loader.load(tmpl_name)

    async def render_template(self, writer, tmpl_name, args=()):
        tmpl = self._load_template(tmpl_name)
        for s in tmpl(*args):
            await writer.awritestr(s)

    def render_str(self, tmpl_name, args=()):
        #TODO: bloat
        tmpl = self._load_template(tmpl_name)
        return ''.join(tmpl(*args))

    def init(self):
        """Initialize a web application. This is for overriding by subclasses.
        This is good place to connect to/initialize a database, for example."""
        self.inited = True

    def serve(self, loop, host="0.0.0.0", port=80):
        # Actually serve client connections. Subclasses may override this
        # to e.g. catch and handle exceptions when dealing with server socket
        # (which are otherwise unhandled and will terminate a Picoweb app).
        # Note: name and signature of this method may change.
        loop.create_task(asyncio.start_server(self._handle, host, port))
#        loop.run_forever()

    def run(self, host, port, debug=False, lazy_init=False, log=None):
        gc.collect()
        self.init()
        if not lazy_init:
            for app in self.mounts:
                app.init()
        loop = asyncio.get_event_loop()
        if debug > 0:
            print("http server running on http://%s:%s/" % (host, port))
        self.serve(loop, host, port)

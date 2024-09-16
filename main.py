from machine import UART
import asyncio
import network
import picoweb
import time
import re
import gc
import sys
import local_config

class App:

    cmd = None
    cpm = None
    ttc = None
    gms = None
    snd = None
    atc = None
    hvg = None
    status = "disconnected"
    timeout = 10
    uart_baudrate = 9600

    def __init__(self):
        self.up = time.time()
        self.uart_rd = UART(0, self.uart_baudrate)
        self.uart_rd.init(self.uart_baudrate, bits=8, parity=None, stop=1, rxbuf=64)
        self.uart_wr = UART(1, self.uart_baudrate)
        self.uart_wr.init(self.uart_baudrate, bits=8, parity=None, stop=1)

    def run(self):
        asyncio.create_task(self.network_init())
        asyncio.run(self.handle_uart())

    async def network_init(self):
        network.country("GB")
        network.hostname("netio-gc10")
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(local_config.ssid, local_config.password)
        print("connecting to", local_config.ssid)
        while not wlan.isconnected():
            await asyncio.sleep_ms(10)
        status = wlan.ifconfig()
        self.ip = status[0]
        print(f'connected, ip = {self.ip}')
        self.start_web_server()

    def start_web_server(self):
        app = picoweb.WebApp(__name__)
        
        @app.route("/")
        async def index(req, resp):
            await picoweb.start_response(resp)
            await resp.awrite("NetIO GC10 v2.4 WebAPI\n")

        def protected(f):
            async def wrapper(req, resp):
                auth = req.headers.get(b'Authorization', b'').decode('ascii').split(' ', 1)
                if auth != ["Bearer", local_config.api_key]:
                    await picoweb.http_error(resp, 401)
                    return
                await f(req, resp)
            return wrapper

        def post(f):
            async def wrapper(req, resp):
                if req.method != "POST":
                    await picoweb.http_error(resp, 405)
                    return
                await req.read_form_data()
                await f(req, resp)
            return wrapper

        @app.route("/cmd/set/atc")
        @protected
        @post
        async def cmd(req, resp):
            value = req.form['value']
            value = int(value)
            self.set_atc(value)
            await picoweb.start_response(resp)
            await resp.awrite("Ok\n")

        @app.route("/cmd/set/snd")
        @protected
        @post
        async def cmd(req, resp):
            value = req.form['value']
            if value in ('on', 'off'):
                self.set_snd(1 if value == 'on' else 0)
            else:
                await picoweb.http_error(resp, 400)
                return
            await picoweb.start_response(resp)
            await resp.awrite("Ok\n")

        @app.route("/cmd/set/gms")
        @protected
        @post
        async def cmd(req, resp):
            value = req.form['value']
            value = int(value)
            self.set_gms(value)
            await picoweb.start_response(resp)
            await resp.awrite("Ok\n")

        @app.route("/cmd/save")
        @protected
        @post
        async def cmd(req, resp):
            self.save()
            await picoweb.start_response(resp)
            await resp.awrite("Ok\n")

        @app.route("/status")
        @protected
        async def status(req, resp):
            self.get_vars()
            await picoweb.jsonify(resp, {
                "status": self.status,
                "uptime": time.time() - self.up,
                "cpm": self.cpm,
                "ttc": self.ttc,
                "gms": self.gms,
                "atc": self.atc,
                "hvg": self.hvg,
                "snd": self.snd,
            })

        app.run(host=self.ip, port=80)

    def get_vars(self):
        self.uart_send("show")
        while True:
            line = self.uart_readline()
            self.process_line(line)
            if line[:4] == 'hvg:':
                break

    def set_gms(self, value):
        self.uart_send(f"set gms={value}")

    def set_snd(self, value):
        self.uart_send(f"set snd={'on' if value else 'off'}")
        self.snd = value

    def set_atc(self, value):
        self.uart_send(f"set atc={value}")

    def save(self):
        self.uart_send(f"save")

    def uart_send(self, c):
        # print("sending:", c)
        self.uart_wr.write(bytes(c + '\r\n', 'ascii'))

    def process_line(self, line):
        # print("received:", line)
        if '0' <= line[:1] <= '9':
            value = int(line)
            self.cpm = value
        elif line[:5] == 'ttc: ':
            value = int(line[5:])
            self.ttc = value
        elif line[:5] == 'gms: ':
            value = int(line[5:])
            self.gms = value
        elif line[:5] == 'atc: ':
            value = int(line[5:])
            self.atc = value
        elif line[:5] == 'hvg: ':
            value = int(line[5:])
            self.hvg = value

    def uart_readline(self):
        line = ''
        while True:
            while not self.uart_rd.any():
                pass
            try:
                ch = self.uart_rd.read(1).decode('ascii')
            except UnicodeError:
                continue
            if ch == '\r':
                continue
            if ch == '\n':
                break
            line += ch
        return line
 
    async def handle_uart(self):
#        self.uart_send('')
        last_line = None
        gc_done = None
        while True:
            now = time.time()
            if self.uart_rd.any():
                line = self.uart_readline()
                last_line = now
                self.process_line(line)
                continue
            if last_line is not None:
                self.status = "ok" if now - last_line < self.timeout else "disconected"
            if gc_done != now:
                gc.collect()
                gc_done = now
            await asyncio.sleep_ms(1)


App().run()



from flask import Flask, render_template_string, request, redirect, url_for, flash, Response, session, make_response, send_file, jsonify
import sqlite3
import csv
import io
import calendar
import random
from datetime import datetime, timedelta
from fpdf import FPDF

app = Flask(__name__)
app.secret_key = 'clave_secreta_super_segura' 

# --- CONFIGURACIÓN ---
PASSWORD_ADMIN = "admin123" 

# --- BASE DE DATOS ---
def get_db_connection():
    conn = sqlite3.connect('prestamos.db')
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON") 
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY, nombre TEXT UNIQUE, cedula TEXT, telefono TEXT, pin TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS prestamos (id INTEGER PRIMARY KEY, cliente_id INTEGER, monto_original REAL, tasa_mensual REAL, duracion_meses INTEGER, frecuencia_pago TEXT, num_cuotas INTEGER, monto_cuota REAL, total_pagar REAL, fecha_inicio TEXT, estado_prestamo TEXT DEFAULT 'ACTIVO', aplica_seguro INTEGER DEFAULT 0, FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pagos (id INTEGER PRIMARY KEY, prestamo_id INTEGER, monto REAL, fecha TEXT, FOREIGN KEY(prestamo_id) REFERENCES prestamos(id) ON DELETE CASCADE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS retiros_fondo (id INTEGER PRIMARY KEY, monto REAL, fecha TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS cargos_extra (id INTEGER PRIMARY KEY, prestamo_id INTEGER, monto REAL, motivo TEXT, fecha TEXT, FOREIGN KEY(prestamo_id) REFERENCES prestamos(id) ON DELETE CASCADE)''')
    try: cursor.execute("ALTER TABLE prestamos ADD COLUMN estado_prestamo TEXT DEFAULT 'ACTIVO'")
    except: pass
    try: cursor.execute("ALTER TABLE prestamos ADD COLUMN aplica_seguro INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE clientes ADD COLUMN pin TEXT"); cursor.execute("UPDATE clientes SET pin = '0000' WHERE pin IS NULL")
    except: pass
    conn.commit(); conn.close()

init_db()

# --- RECIBOS PDF ---
class ReciboPDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16); self.cell(0, 10, 'COMPROBANTE DE PAGO', 0, 1, 'C'); self.ln(5)
    def footer(self):
        self.set_y(-15); self.set_font('Arial', 'I', 8); self.cell(0, 10, 'Sistema PrestamosApp Ni - Gracias por su pago', 0, 0, 'C')

# --- LÓGICA DE NEGOCIO ---
def obtener_ultimo_dia_mes(anio, mes): return calendar.monthrange(anio, mes)[1]
def calcular_siguiente_quincena(fecha_actual):
    if fecha_actual.day < 15: return datetime(fecha_actual.year, fecha_actual.month, 15)
    ultimo_dia = obtener_ultimo_dia_mes(fecha_actual.year, fecha_actual.month)
    dia_fin = 30 if ultimo_dia >= 30 else ultimo_dia
    if fecha_actual.day < dia_fin: return datetime(fecha_actual.year, fecha_actual.month, dia_fin)
    if fecha_actual.month == 12: return datetime(fecha_actual.year + 1, 1, 15)
    else: return datetime(fecha_actual.year, fecha_actual.month + 1, 15)

def generar_plan_pagos(prestamo, total_pagado):
    try: fecha_inicio = datetime.strptime(prestamo['fecha_inicio'], '%Y-%m-%d %H:%M:%S.%f')
    except: fecha_inicio = datetime.strptime(prestamo['fecha_inicio'], '%Y-%m-%d %H:%M:%S')
    frecuencia = prestamo['frecuencia_pago']; cuota_base = round(prestamo['monto_cuota'], 2); num_cuotas = prestamo['num_cuotas']
    total_deuda = round(prestamo['total_pagar'], 2); total_cap = round(cuota_base * num_cuotas, 2)
    mora_global = round(total_deuda - total_cap, 2) if (total_deuda - total_cap) > 0.05 else 0
    dinero = round(total_pagado, 2); plan = []
    
    if frecuencia == 'semanal':
        ds = fecha_inicio.weekday(); dias = 5 - ds if ds <= 2 else (5 - ds) + 7
        proxima = fecha_inicio + timedelta(days=dias)
    elif frecuencia == 'quincenal': proxima = calcular_siguiente_quincena(fecha_inicio)
    else: proxima = fecha_inicio + timedelta(days=30)

    # Lógica Capital Rey
    pagadas_full = 0; temp = dinero
    for i in range(1, num_cuotas + 1):
        if temp >= (cuota_base - 0.05): temp = round(temp - cuota_base, 2); pagadas_full += 1
        else: break
    
    sobra_mora = temp
    if mora_global > 0:
        pagado_mora = min(mora_global, sobra_mora); mora_viva = round(mora_global - pagado_mora, 2); sobra_mora = round(sobra_mora - pagado_mora, 2)
    else: mora_viva = 0
    
    abono_cap = sobra_mora; mora_pintada = False

    for i in range(1, num_cuotas + 1):
        monto = cuota_base; est = "PENDIENTE"; cls = "pending"; det = ""; es_pend = False
        if i <= pagadas_full: est = "PAGADO"; cls = "paid"
        else:
            es_pend = True
            if not mora_pintada:
                monto = round(cuota_base + mora_viva, 2)
                if mora_viva > 0.05: det = f'<div style="color:#c0392b; font-weight:bold; font-size:0.85rem; margin-top:2px;">⚠️ + Mora: C${mora_viva:,.2f}</div>'
                if abono_cap > 0.05:
                    falta = round(monto - abono_cap, 2); est = f"PARCIAL (Resta C${falta:,.2f})"
                    cls = "mora-row" if mora_viva > 0.05 else "partial"
                    det += f'<div style="color:#16a085; font-size:0.8rem;">✓ Abonado: C${abono_cap:,.2f}</div>'
                else:
                    est = "PENDIENTE"; cls = "mora-row" if mora_viva > 0.05 else "pending"
                mora_pintada = True
            else: est = "PENDIENTE"; cls = "pending"
        
        plan.append({'numero': i, 'fecha': proxima.strftime('%d/%m/%Y'), 'monto': monto, 'estado': est, 'clase': cls, 'detalle': det, 'es_pendiente': es_pend, 'tiene_mora': (mora_viva > 0.05 and mora_pintada and es_pend)})
        
        if frecuencia == 'semanal': proxima += timedelta(weeks=1)
        elif frecuencia == 'quincenal':
            if proxima.day <= 15: ud = obtener_ultimo_dia_mes(proxima.year, proxima.month); df = 30 if ud >= 30 else ud; proxima = datetime(proxima.year, proxima.month, df)
            else: proxima = datetime(proxima.year + 1, 1, 15) if proxima.month == 12 else datetime(proxima.year, proxima.month + 1, 15)
        else: proxima += timedelta(days=30)
    return plan

# --- PLANTILLA HTML (PWA READY + CORRECCIÓN VISUAL) ---
HTML_CABECERA = """
<!DOCTYPE html>
<html lang="es" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#0f3460">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="PrestamosApp">
    <link rel="manifest" href="/manifest.json">
    <link rel="apple-touch-icon" href="https://img.icons8.com/color/48/banknotes.png">
    
    <title>PrestamosApp Ni</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --primary: #0f3460; --accent: #e94560; --success: #27ae60; --danger: #c0392b; --warning: #f39c12; --info: #16213e; --bg: #f2f2f2; --card-bg: #ffffff; --text: #1a1a2e; --border: #dcdcdc; --muted: #666; --table-head: #e8e8e8; --table-row: #f9f9f9; }
        [data-theme="dark"] { --primary: #3282b8; --accent: #ff2e63; --success: #00b894; --danger: #ff7675; --warning: #ffeaa7; --info: #0f3460; --bg: #0f172a; --card-bg: #1e293b; --text: #e2e8f0; --border: #334155; --muted: #94a3b8; --table-head: #334155; --table-row: #1e293b; }
        * { box-sizing: border-box; } body { font-family: 'Inter', sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 0; transition: background-color 0.3s; }
        
        .navbar { background-color: var(--info); padding: 1rem; display: flex; justify-content: space-between; align-items: center; color: white; position: sticky; top: 0; z-index: 1000; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2); }
        .navbar-brand { font-weight: 800; font-size: 1.2rem; display: flex; align-items: center; gap: 8px; }
        .navbar-menu a { color: #e2e8f0; text-decoration: none; margin-left: 15px; font-weight: 500; }
        .container { max-width: 1100px; margin: 1.5rem auto; padding: 0 1rem; padding-bottom: 80px; }
        .card { background: var(--card-bg); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 3px 6px rgba(0,0,0,0.08); border: 1px solid var(--border); }
        .card h3 { margin-top: 0; color: var(--primary); border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-bottom: 15px; font-weight: 700; }
        
        label { display: block; margin-bottom: 5px; font-size: 0.8rem; font-weight: 600; color: var(--muted); }
        input, select { width: 100%; padding: 10px; margin-bottom: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--card-bg); color: var(--text); font-size: 15px; }
        
        .btn { display: inline-flex; justify-content: center; align-items: center; padding: 10px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; cursor: pointer; border: none; font-size: 0.9rem; gap: 5px; }
        .btn-primary { background-color: var(--primary); color: white; } .btn-success { background-color: var(--success); color: white; } .btn-danger { background-color: var(--danger); color: white; } .btn-warning { background-color: var(--warning); color: #1a1a2e; } .btn-info { background-color: var(--info); color: white; } .btn-whatsapp { background-color: #25D366; color: white; } .btn-outline { border: 1px solid var(--border); color: var(--text); background: var(--card-bg); } .btn-full { width: 100%; } .btn-sm { padding: 6px 10px; font-size: 0.75rem; }
        
        .search-bar { display: flex; gap: 8px; margin-bottom: 15px; }
        .table-wrapper { overflow-x: auto; max-height: 350px; overflow-y: auto; border-radius: 8px; border: 1px solid var(--border); }
        table { width: 100%; border-collapse: collapse; min-width: 100%; }
        thead th { position: sticky; top: 0; background-color: var(--table-head); z-index: 10; text-align: left; padding: 10px; font-size: 0.75rem; color: var(--text); font-weight: 700; text-transform: uppercase; border-bottom: 2px solid var(--border); }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 0.85rem; color: var(--text); vertical-align: middle; }
        tbody tr:nth-child(even) { background-color: var(--table-row); }
        
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; } .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; } .grid-4-dashboard { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }
        .stat-card { text-align: center; padding: 15px; color: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .stat-label { font-size: 0.75rem; opacity: 0.9; text-transform: uppercase; margin-bottom: 5px; } .stat-number { font-size: 1.6rem; font-weight: 800; margin: 0; }
        
        .badge { padding: 4px 10px; border-radius: 12px; font-size: 0.7rem; font-weight: 700; } .paid { background: #d4edda; color: #155724; } .pending { background: #f8d7da; color: #721c24; } .partial { background: #fff3cd; color: #856404; } .mora-row { color: var(--danger); font-weight: bold; background: rgba(192, 57, 43, 0.1); border-left: 4px solid var(--danger); }
        
        .theme-toggle { position: fixed; bottom: 20px; right: 20px; background: var(--primary); color: white; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.3); border: none; z-index: 2000; }
        .greeting-anim { animation: fadeIn 2s ease-in-out; font-size: 1.2rem; font-weight: 300; color: #e2e8f0; margin-right: 15px; }
        @keyframes fadeIn { 0% { opacity: 0; transform: translateY(-10px); } 100% { opacity: 1; transform: translateY(0); } }
        
        .summary-card { background: linear-gradient(135deg, var(--primary) 0%, var(--info) 100%); color: white; padding: 25px; border-radius: 15px; margin-bottom: 25px; box-shadow: 0 5px 15px rgba(0,0,0,0.2); }
        .summary-amount { font-size: 2.5rem; font-weight: 800; margin: 0; }
        .alert-mora { background: linear-gradient(135deg, #c0392b 0%, #e74c3c 100%); } .alert-ok { background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%); }
        
        /* AQUÍ ESTÁ EL ARREGLO VISUAL */
        .print-header { display: none; }
        
        @media (max-width: 768px) { .grid-2, .grid-3, .navbar, .grid-4-dashboard { grid-template-columns: 1fr; flex-direction: column; display: flex; } .navbar-menu { width: 100%; justify-content: space-around; margin-top: 10px; } .grid-3 { gap: 10px; } .greeting-anim { display: none; } }
        
        @media print { 
            .no-print, .search-bar, .btn, .theme-toggle { display: none !important; } 
            .card { border: none; shadow: none; padding: 0; margin-bottom: 0; background: white; color: black; } 
            .hide-on-print { display: none !important; } 
            body { background: white; color: black; font-size: 11pt; } 
            .navbar { display: none; } 
            .table-wrapper { max-height: none; overflow: visible; border: none; } 
            .print-header { display: block !important; text-align: center; margin-bottom: 20px; border-bottom: 2px solid #000; padding-bottom: 10px; } 
            .stat-card { color: black !important; background: white !important; border: 1px solid #ccc; } 
            [data-theme="dark"] { --text: black; --bg: white; --card-bg: white; } 
        }
    </style>
</head>
<body>
    <button class="theme-toggle no-print" onclick="toggleTheme()" id="themeBtn">🌙</button>
    <div class="print-header"><h1>Reporte Financiero</h1><p>Fecha: <script>document.write(new Date().toLocaleDateString())</script></p></div>
    <nav class="navbar no-print">
        <div class="navbar-brand">🇳🇮 PrestamosApp</div>
        <div style="display:flex; align-items:center;">
            <div id="greeting" class="greeting-anim"></div>
            <div class="navbar-menu">
                <a href="/">Inicio</a>
                {% if session.get('admin_logged_in') %} <a href="/logout">Salir</a>
                {% else %} <a href="/cliente_login">Clientes</a> {% endif %}
            </div>
        </div>
    </nav>
    <div class="container">
    {% with messages = get_flashed_messages() %} {% if messages %} {% for message in messages %} <div class="no-print" style="padding:10px; background:#2ecc71; color:white; border-radius:6px; margin-bottom:20px;">{{ message }}</div> {% endfor %} {% endif %} {% endwith %}
"""

HTML_PIE = """</div>
    <div class="no-print" style="text-align: center; padding: 20px; color: var(--muted); font-size: 0.8rem;">&copy; 2026 Sistema Nicaragua</div>
    <script>
        const htmlEl = document.documentElement; const btn = document.getElementById('themeBtn');
        if (localStorage.getItem('theme') === 'dark') { htmlEl.setAttribute('data-theme', 'dark'); btn.textContent = '☀️'; }
        function toggleTheme() { if (htmlEl.getAttribute('data-theme') === 'dark') { htmlEl.setAttribute('data-theme', 'light'); localStorage.setItem('theme', 'light'); btn.textContent = '🌙'; } else { htmlEl.setAttribute('data-theme', 'dark'); localStorage.setItem('theme', 'dark'); btn.textContent = '☀️'; } }
        document.addEventListener('DOMContentLoaded', function() {
            const h = new Date().getHours(); const el = document.getElementById('greeting');
            let m = h < 12 ? 'Buenos días ☀️' : h < 19 ? 'Buenas tardes 🌤️' : 'Buenas noches 🌙';
            if(el) el.textContent = m;
        });
    </script>
</body></html>"""

# --- RUTA MANIFEST.JSON (PARA PWA) ---
@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "PrestamosApp Ni",
        "short_name": "Prestamos",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f3460",
        "theme_color": "#0f3460",
        "icons": [
            {
                "src": "https://img.icons8.com/color/192/banknotes.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "https://img.icons8.com/color/512/banknotes.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    })

# --- RESTO DE RUTAS ---
@app.route('/')
def home(): return render_template_string(HTML_CABECERA + """<div style="text-align: center; padding: 50px 0;"><h1 style="color: var(--primary); font-size: 2.5rem; margin-bottom: 10px;">Gestión de Préstamos</h1><div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap;"><a href='/admin_login' class="btn btn-primary btn-lg">Acceso Administrador</a><a href='/cliente_login' class="btn btn-outline btn-lg">Acceso Cliente</a></div></div>""" + HTML_PIE)

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form['password'] == PASSWORD_ADMIN: session['admin_logged_in'] = True; return redirect('/admin')
        else: flash('Contraseña incorrecta')
    return render_template_string(HTML_CABECERA + """<div class="card" style="max-width:400px; margin:40px auto; text-align:center;"><h3>🔒 Seguridad</h3><form method="post"><input type="password" name="password" placeholder="Contraseña" required><button type="submit" class="btn btn-primary btn-full">Ingresar</button></form></div>""" + HTML_PIE)

@app.route('/logout')
def logout(): session.pop('admin_logged_in', None); return redirect('/')

@app.route('/recibo/<int:pago_id>')
def descargar_recibo(pago_id):
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute('''SELECT pg.id as pago_id, pg.monto, pg.fecha, c.nombre, c.cedula, p.id as prestamo_id FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id JOIN clientes c ON p.cliente_id = c.id WHERE pg.id = ?''', (pago_id,))
    data = cursor.fetchone(); conn.close()
    if not data: return redirect('/')
    pdf = ReciboPDF(); pdf.add_page(); pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, f"Fecha: {data['fecha'][:19]}", 0, 1); pdf.cell(0, 10, f"Recibo #: {data['pago_id']:06d}", 0, 1); pdf.ln(10)
    pdf.set_font("Arial", 'B', 12); pdf.cell(40, 10, "Cliente:", 0, 0); pdf.set_font("Arial", '', 12); pdf.cell(0, 10, f"{data['nombre']} ({data['cedula']})", 0, 1)
    pdf.set_font("Arial", 'B', 12); pdf.cell(40, 10, "Prestamo ID:", 0, 0); pdf.set_font("Arial", '', 12); pdf.cell(0, 10, f"#{data['prestamo_id']}", 0, 1)
    pdf.ln(10); pdf.set_fill_color(240, 240, 240); pdf.set_font("Arial", 'B', 14)
    pdf.cell(0, 15, f"MONTO PAGADO: C$ {data['monto']:.2f}", 1, 1, 'C', fill=True)
    response = make_response(pdf.output(dest='S').encode('latin-1'))
    response.headers.set('Content-Disposition', 'attachment', filename=f'recibo_{data["pago_id"]}.pdf'); response.headers.set('Content-Type', 'application/pdf')
    return response

@app.route('/descargar_backup')
def descargar_backup():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    try: return send_file('prestamos.db', as_attachment=True, download_name=f"backup_{datetime.now().strftime('%Y%m%d')}.db")
    except: flash("Error al descargar backup"); return redirect('/admin')

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'): return redirect('/admin_login') 
    search_query = request.args.get('q', '')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM pagos WHERE prestamo_id NOT IN (SELECT id FROM prestamos)"); cursor.execute("DELETE FROM cargos_extra WHERE prestamo_id NOT IN (SELECT id FROM prestamos)"); cursor.execute("DELETE FROM prestamos WHERE cliente_id NOT IN (SELECT id FROM clientes)"); conn.commit()
    if search_query: cursor.execute("SELECT * FROM clientes WHERE nombre LIKE ? ORDER BY nombre", ('%' + search_query + '%',))
    else: cursor.execute("SELECT * FROM clientes ORDER BY nombre")
    clientes = cursor.fetchall()
    cursor.execute("SELECT COALESCE(SUM(monto_original), 0) FROM prestamos"); capital_colocado_total = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(monto),0) FROM pagos"); total_recaudado = cursor.fetchone()[0]
    cursor.execute('''SELECT COALESCE(SUM(pg.monto), 0) FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id WHERE p.aplica_seguro = 1'''); recaudado_con_seguro = cursor.fetchone()[0]; fondo_disponible = (recaudado_con_seguro * 0.02)
    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM retiros_fondo"); total_retirado = cursor.fetchone()[0]; fondo_disponible -= total_retirado
    cursor.execute('''SELECT pg.monto, p.monto_original, p.total_pagar, p.aplica_seguro FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id''')
    pagos_calc = cursor.fetchall(); ganancia_neta = 0
    for pg in pagos_calc:
        monto = pg[0]; cap = pg[1]; tot = pg[2]; seg = pg[3]
        if tot > 0:
            interes_total = tot - cap; ratio = interes_total / tot; ganancia_neta += (monto * ratio)
            if seg: ganancia_neta -= (monto * (cap*0.02*(tot/cap)/tot))
    lista_reporte_clientes = []
    for cl in clientes:
        cursor.execute('''SELECT COALESCE(SUM(monto_original),0), COALESCE(SUM(total_pagar),0) FROM prestamos WHERE cliente_id=?''', (cl['id'],)); res_prest = cursor.fetchone()
        cursor.execute('''SELECT COALESCE(SUM(pg.monto),0) FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id WHERE p.cliente_id=?''', (cl['id'],)); pagado_hist = cursor.fetchone()[0]
        lista_reporte_clientes.append({'nombre': cl['nombre'], 'prestado': res_prest[0], 'pagado': pagado_hist, 'saldo': res_prest[1] - pagado_hist})
    conn.close()
    return render_template_string(HTML_CABECERA + """
    <div class="card" style="margin-bottom:20px;"><div style="display:flex; justify-content:space-between; align-items:center;"><h3>📊 Resumen Financiero</h3><a href="/descargar_backup" class="btn btn-outline btn-sm">💾 Respaldar Datos</a></div><div style="height:200px;"><canvas id="financeChart"></canvas></div></div>
    <script>const ctx = document.getElementById('financeChart').getContext('2d'); new Chart(ctx, { type: 'bar', data: { labels: ['Capital Prestado', 'Total Recaudado', 'Ganancia'], datasets: [{ label: 'Montos (C$)', data: [{{ capital_colocado_total }}, {{ total_recaudado }}, {{ ganancia_neta }}], backgroundColor: ['#0f3460', '#27ae60', '#e94560'] }] }, options: { responsive: true, maintainAspectRatio: false } });</script>
    <div class="grid-4-dashboard">
        <div class="stat-card" style="background:var(--primary);"><div class="stat-label">Capital</div><div class="stat-number">C${{ "{:,.2f}".format(capital_colocado_total) }}</div></div>
        <div class="stat-card" style="background:var(--success);"><div class="stat-label">Recaudado</div><div class="stat-number">C${{ "{:,.2f}".format(total_recaudado) }}</div></div>
        <div class="stat-card" style="background:#20bf6b;"><div class="stat-label">Ganancia</div><div class="stat-number">C${{ "{:,.2f}".format(ganancia_neta) }}</div></div>
        <div class="stat-card" style="background:var(--info);"><div class="stat-label">Fondo 2%</div><div class="stat-number">C${{ "{:,.2f}".format(fondo_disponible) }}</div>{% if fondo_disponible > 0.1 %}<form action="/vaciar_fondo" method="post" onsubmit="return confirm('¿Retirar?');"><input type="hidden" name="monto" value="{{ fondo_disponible }}"><button type="submit" class="btn btn-warning btn-sm" style="margin-top:8px; width:100%;">🗑️ Retirar</button></form>{% endif %}</div>
    </div>
    <div class="grid-2 main-layout hide-on-print">
        <div class="card" style="border-top: 4px solid var(--accent);"><h3>💰 Crear Préstamo</h3><form action="/add_prestamo" method="post"><select name="cliente_id" required><option value="">Seleccionar Cliente...</option>{% for c in clientes_list %}<option value="{{ c['id'] }}">{{ c['nombre'] }}</option>{% endfor %}</select><div class="grid-2"><div><label>Monto</label><input type="number" step="0.01" name="monto" required></div><div><label>Tasa (%)</label><input type="number" step="0.01" name="tasa" required></div></div><div class="grid-2"><div><label>Fecha Inicio</label><input type="date" name="fecha_custom"></div><div style="display:flex; align-items:center; margin-top:20px;"><input type="checkbox" name="seguro" value="1" style="width:20px; margin:0 10px 0 0;"><label style="margin:0;">¿2%?</label></div></div><div class="grid-2"><div><label>Meses</label><input type="number" name="meses" required></div><div><label>Frecuencia</label><select name="frecuencia"><option value="semanal">Semanal</option><option value="quincenal">Quincenal</option><option value="mensual">Mensual</option></select></div></div><button type="submit" class="btn btn-primary btn-full">Crear Préstamo</button></form></div>
        <div class="card"><h3>👥 Clientes</h3><form action="/admin" method="get" class="search-bar no-print"><input type="text" name="q" placeholder="Buscar..." value="{{ query }}"><button type="submit" class="btn btn-primary">🔍</button></form><form action="/add_cliente" method="post" class="hide-on-print" style="margin-bottom:15px; border-bottom:1px solid #eee; padding-bottom:15px;"><div class="grid-2"><input type="text" name="nombre" placeholder="Nombre" required><input type="text" name="telefono" placeholder="Teléfono"></div><input type="text" name="cedula" placeholder="Cédula" style="margin-top:10px;"><button type="submit" class="btn btn-success btn-sm btn-full" style="margin-top:10px;">+ Nuevo Cliente</button></form><div class="table-wrapper"><table><thead><tr><th>Nombre</th><th>Teléfono</th><th>PIN</th><th class="no-print">Acción</th></tr></thead><tbody>{% for c in clientes_list %}<tr><td><div style="font-weight:600;">{{ c['nombre'] }}</div><div style="font-size:0.75rem;">{{ c['cedula'] }}</div></td><td>{{ c['telefono'] }}</td><td style="font-weight:bold; color:var(--accent);">{{ c['pin'] }}</td><td class="no-print"><a href="/editar_cliente/{{ c['id'] }}" class="btn btn-outline btn-sm">✏️</a><a href="/admin/cliente/{{ c['id'] }}" class="btn btn-primary btn-sm">Ver</a></td></tr>{% endfor %}</tbody></table></div></div>
    </div>
    """ + HTML_PIE, clientes_list=clientes, query=search_query, capital_colocado_total=capital_colocado_total, total_recaudado=total_recaudado, ganancia_neta=ganancia_neta, fondo_disponible=fondo_disponible, reporte_cli=lista_reporte_clientes)

@app.route('/vaciar_fondo', methods=['POST'])
def vaciar_fondo():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO retiros_fondo (monto, fecha) VALUES (?, ?)", (request.form['monto'], datetime.now())); conn.commit(); conn.close()
    flash('Fondo retirado'); return redirect('/admin')

@app.route('/editar_cliente/<int:id>', methods=['GET', 'POST'])
def editar_cliente(id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    if request.method == 'POST':
        cursor.execute("UPDATE clientes SET nombre=?, cedula=?, telefono=?, pin=? WHERE id=?", (request.form['nombre'], request.form['cedula'], request.form['telefono'], request.form['pin'], id)); conn.commit(); conn.close(); flash('Actualizado'); return redirect('/admin')
    cursor.execute("SELECT * FROM clientes WHERE id=?", (id,)); cliente = cursor.fetchone(); conn.close()
    return render_template_string(HTML_CABECERA + """<div class="card" style="max-width:500px; margin:20px auto;"><h3>✏️ Editar</h3><form method="post"><label>Nombre</label><input type="text" name="nombre" value="{{ c['nombre'] }}" required><label>Cédula</label><input type="text" name="cedula" value="{{ c['cedula'] }}"><label>Teléfono</label><input type="text" name="telefono" value="{{ c['telefono'] }}"><label>PIN</label><input type="text" name="pin" value="{{ c['pin'] }}"><button type="submit" class="btn btn-primary btn-full" style="margin-top:20px;">Guardar</button></form></div>""" + HTML_PIE, c=cliente)

@app.route('/admin/cliente/<int:cliente_id>')
def detalle_cliente(cliente_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)); cliente = cursor.fetchone()
    cursor.execute('''SELECT p.*, COALESCE(SUM(pg.monto), 0) as pagado FROM prestamos p LEFT JOIN pagos pg ON p.id = pg.prestamo_id WHERE p.cliente_id = ? GROUP BY p.id ORDER BY p.estado_prestamo ASC, p.id ASC''', (cliente_id,)); prestamos = cursor.fetchall(); conn.close()
    contenido = """
    <div class="no-print" style="display:flex; justify-content:space-between; margin-bottom:15px;"><a href="/admin" class="btn btn-outline btn-sm">← Volver</a><div style="display:flex; gap:10px;">{% if cliente['telefono'] %}<a href="https://wa.me/505{{ cliente['telefono']|replace('-','')|replace(' ','') }}?text=Hola {{ cliente['nombre'] }}, tiene una cuota pendiente." target="_blank" class="btn btn-whatsapp btn-sm">📲 WhatsApp</a>{% endif %}<button onclick="window.print()" class="btn btn-primary btn-sm">🖨️ Ficha</button><a href="/delete_cliente/{{ cliente['id'] }}" onclick="return confirm('¿Borrar TODO?')" class="btn btn-danger btn-sm">🗑️</a></div></div>
    <div class="card"><h2>{{ cliente['nombre'] }}</h2><small>Tel: {{ cliente['telefono'] }} | PIN: <strong>{{ cliente['pin'] }}</strong></small></div>
    {% for p in prestamos %}{% set deuda = p['total_pagar'] - p['pagado'] %}
    <div class="card" style="border-left: 5px solid {% if p['estado_prestamo']=='REFINANCIADO' %}#95a5a6{% elif deuda < 1 %}var(--success){% else %}var(--warning){% endif %}; position:relative;">
        <a href="/delete_prestamo/{{ p['id'] }}" onclick="return confirm('¿Borrar este préstamo?')" class="no-print" style="position:absolute; top:15px; right:15px; text-decoration:none; font-size:1.2rem;">🗑️</a>
        <div style="margin-bottom:10px;"><strong>Préstamo #{{ loop.index }}</strong> {% if deuda < 1 %}<span class="badge paid">PAGADO</span>{% else %}<span class="badge pending">ACTIVO</span>{% endif %}</div>
        <div class="grid-2"><div>Deuda: <strong>C${{ "{:,.2f}".format(p['total_pagar']) }}</strong></div><div>Resta: <strong style="color:var(--danger);">C${{ "{:,.2f}".format(deuda) }}</strong></div></div>
        {% if p['estado_prestamo'] == 'ACTIVO' and deuda > 0.1 %}
        <hr style="margin:15px 0; border-color:var(--border);" class="no-print">
        <div class="grid-2 no-print"><form action="/aplicar_mora/{{ p['id'] }}" method="post" onsubmit="return confirm('¿Aplicar Mora?');"><input type="hidden" name="monto_cuota" value="{{ p['monto_cuota'] }}"><button type="submit" class="btn btn-warning btn-sm btn-full">⚠️ Mora (3%)</button></form><a href="/refinanciar/{{ p['id'] }}" onclick="return confirm('¿Refinanciar?')" class="btn btn-primary btn-sm btn-full">🔄 Refinanciar</a></div>
        <form action="/add_pago" method="post" class="no-print" style="margin-top:10px; display:flex; gap:10px;"><input type="hidden" name="prestamo_id" value="{{ p['id'] }}"><input type="hidden" name="origen" value="detalle"><input type="hidden" name="cliente_id" value="{{ cliente['id'] }}"><input type="number" step="0.01" name="monto_pago" placeholder="Monto (C$)" required><button type="submit" class="btn btn-success">Pagar</button></form>
        {% endif %}
        <div style="margin-top:10px;" class="no-print"><a href="/ver_plan/{{ p['id'] }}" class="btn btn-outline btn-sm btn-full">📅 Tabla Pagos</a></div>
    </div>{% endfor %}"""
    return render_template_string(HTML_CABECERA + contenido + HTML_PIE, cliente=cliente, prestamos=prestamos)

@app.route('/aplicar_mora/<int:prestamo_id>', methods=['POST'])
def aplicar_mora(prestamo_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    monto = float(request.form['monto_cuota']) * 0.03
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE prestamos SET total_pagar = total_pagar + ? WHERE id=?", (monto, prestamo_id))
    cursor.execute("INSERT INTO cargos_extra (prestamo_id, monto, motivo, fecha) VALUES (?, ?, ?, ?)", (prestamo_id, monto, "MORA 3%", datetime.now()))
    conn.commit(); conn.close(); flash('Mora aplicada'); return redirect(request.referrer)

@app.route('/refinanciar/<int:prestamo_id>')
def refinanciar(prestamo_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT * FROM prestamos WHERE id=?", (prestamo_id,)); p = cursor.fetchone()
    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE prestamo_id=?", (prestamo_id,)); pagado = cursor.fetchone()[0]
    saldo = p['total_pagar'] - pagado
    cursor.execute("UPDATE prestamos SET estado_prestamo = 'REFINANCIADO' WHERE id=?", (prestamo_id,)); conn.commit(); conn.close()
    return render_template_string(HTML_CABECERA + f"""<div class="card" style="max-width:500px; margin:20px auto;"><h3>🔄 Refinanciar</h3><p>Saldo: <strong>C${saldo:.2f}</strong></p><form action="/add_prestamo" method="post"><input type="hidden" name="cliente_id" value="{p['cliente_id']}"><label>Nuevo Capital</label><input type="number" name="monto" value="{saldo:.2f}" readonly><label>Tasa</label><input type="number" name="tasa" required><div class="grid-2"><div><label>Meses</label><input type="number" name="meses" required></div><div><label>Frecuencia</label><select name="frecuencia"><option value="semanal">Semanal</option><option value="quincenal">Quincenal</option><option value="mensual">Mensual</option></select></div></div><button type="submit" class="btn btn-primary btn-full">Crear</button></form></div>""" + HTML_PIE)

@app.route('/delete_prestamo/<int:id>')
def delete_prestamo(id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM pagos WHERE prestamo_id=?", (id,)); cursor.execute("DELETE FROM cargos_extra WHERE prestamo_id=?", (id,)); cursor.execute("DELETE FROM prestamos WHERE id=?", (id,)); conn.commit(); conn.close(); flash('Préstamo eliminado'); return redirect(request.referrer)

@app.route('/cliente_login')
def cliente_login():
    return render_template_string(HTML_CABECERA + """<div class="card" style="max-width:400px; margin:40px auto; text-align:center;"><h3>👋 Clientes</h3><form action="/cliente_dashboard" method="post"><input type="text" name="nombre" placeholder="Nombre" required><input type="password" name="pin" placeholder="PIN (4 dígitos)" required maxlength="4" style="text-align:center; letter-spacing:5px;"><button type="submit" class="btn btn-primary btn-full">Entrar</button></form></div>""" + HTML_PIE)

@app.route('/cliente_dashboard', methods=['POST'])
def cliente_dashboard():
    nombre = request.form['nombre'].strip(); pin = request.form['pin'].strip()
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM clientes WHERE nombre = ? AND pin = ?", (nombre, pin)); cliente = cursor.fetchone()
    if not cliente: flash('Credenciales incorrectas'); return redirect('/cliente_login')
    cursor.execute('''SELECT p.*, COALESCE(SUM(pg.monto), 0) as pagado FROM prestamos p LEFT JOIN pagos pg ON p.id = pg.prestamo_id WHERE p.cliente_id = ? GROUP BY p.id ORDER BY p.id ASC''', (cliente['id'],)); prestamos = cursor.fetchall(); conn.close()
    
    alerta = None; tipo = 'ok'
    for p in prestamos:
        if (p['total_pagar'] - p['pagado']) > 0.5:
            plan = generar_plan_pagos(p, p['pagado'])
            for c in plan:
                if c['es_pendiente']:
                    if c['tiene_mora']: alerta = {'msg': '⚠️ TIENES UN ATRASO', 'monto': c['monto'], 'fecha': c['fecha']}; tipo = 'danger'
                    elif not alerta: alerta = {'msg': 'Próximo Pago', 'monto': c['monto'], 'fecha': c['fecha']}
                    break
        if tipo == 'danger': break

    return render_template_string(HTML_CABECERA + """
    <div style="display:flex; justify-content:space-between;"><h2>Hola, {{ nombre }}</h2><a href="/cliente_login" class="btn btn-outline btn-sm">Salir</a></div>
    {% if alerta %}<div class="summary-card {% if tipo=='danger' %}alert-mora{% else %}alert-ok{% endif %}"><div class="summary-title">{{ alerta.msg }}</div><div class="summary-amount">C${{ "{:,.2f}".format(alerta.monto) }}</div><div class="summary-date">Fecha: {{ alerta.fecha }}</div></div>
    {% else %}<div class="summary-card alert-ok"><div class="summary-title">ESTADO DE CUENTA</div><div class="summary-amount">C$0.00</div><div class="summary-status">✨ ESTÁS AL DÍA</div></div>{% endif %}
    {% for p in prestamos %}{% set deuda = p['total_pagar'] - p['pagado'] %}
    <div class="card" style="border-left: 5px solid {% if deuda < 1 %}var(--success){% else %}var(--accent){% endif %};">
        <div style="display:flex; justify-content:space-between;"><strong>Préstamo #{{ loop.index }}</strong></div>
        <p>Deuda: C${{ "{:,.2f}".format(p['total_pagar']) }} | Pagado: C${{ "{:,.2f}".format(p['pagado']) }}</p>
        <p style="color:var(--danger)">Resta: C${{ "{:,.2f}".format(deuda) }}</p>
        <a href="/ver_plan/{{ p['id'] }}" class="btn btn-primary btn-sm btn-full">📅 Ver Calendario</a>
    </div>{% else %}<div class="card">No tienes préstamos.</div>{% endfor %}""" + HTML_PIE, nombre=cliente['nombre'], prestamos=prestamos, alerta=alerta, tipo=tipo)

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
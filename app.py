from flask import Flask, render_template_string, request, redirect, url_for, flash, Response, session, make_response, send_file
import sqlite3
import csv
import io
import calendar
import random # Para generar el PIN
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
    # Actualizamos tabla clientes para incluir PIN
    cursor.execute('''CREATE TABLE IF NOT EXISTS clientes 
                 (id INTEGER PRIMARY KEY, nombre TEXT UNIQUE, cedula TEXT, telefono TEXT, pin TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS prestamos 
                 (id INTEGER PRIMARY KEY, cliente_id INTEGER, 
                  monto_original REAL, tasa_mensual REAL, duracion_meses INTEGER,
                  frecuencia_pago TEXT, num_cuotas INTEGER, monto_cuota REAL,
                  total_pagar REAL, fecha_inicio TEXT, 
                  estado_prestamo TEXT DEFAULT 'ACTIVO',
                  aplica_seguro INTEGER DEFAULT 0,
                  FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS pagos 
                 (id INTEGER PRIMARY KEY, prestamo_id INTEGER, monto REAL, fecha TEXT,
                  FOREIGN KEY(prestamo_id) REFERENCES prestamos(id) ON DELETE CASCADE)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS retiros_fondo 
                 (id INTEGER PRIMARY KEY, monto REAL, fecha TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS cargos_extra 
                 (id INTEGER PRIMARY KEY, prestamo_id INTEGER, monto REAL, motivo TEXT, fecha TEXT,
                  FOREIGN KEY(prestamo_id) REFERENCES prestamos(id) ON DELETE CASCADE)''')
    
    # Migraciones para versiones anteriores
    try: cursor.execute("ALTER TABLE prestamos ADD COLUMN estado_prestamo TEXT DEFAULT 'ACTIVO'")
    except: pass
    try: cursor.execute("ALTER TABLE prestamos ADD COLUMN aplica_seguro INTEGER DEFAULT 0")
    except: pass
    try: 
        cursor.execute("ALTER TABLE clientes ADD COLUMN pin TEXT")
        # Si agregamos la columna ahora, pongamos un PIN por defecto '0000' a los viejos
        cursor.execute("UPDATE clientes SET pin = '0000' WHERE pin IS NULL")
    except: pass
    
    conn.commit()
    conn.close()

init_db()

# --- CLASE PARA RECIBOS PDF ---
class ReciboPDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 16)
        self.cell(0, 10, 'COMPROBANTE DE PAGO', 0, 1, 'C')
        self.ln(5)
    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, 'Sistema PrestamosApp Ni - Gracias por su pago', 0, 0, 'C')

# --- LÓGICA DE FECHAS ---
def obtener_ultimo_dia_mes(anio, mes):
    return calendar.monthrange(anio, mes)[1]

def calcular_siguiente_quincena(fecha_actual):
    if fecha_actual.day < 15:
        return datetime(fecha_actual.year, fecha_actual.month, 15)
    ultimo_dia = obtener_ultimo_dia_mes(fecha_actual.year, fecha_actual.month)
    dia_fin = 30 if ultimo_dia >= 30 else ultimo_dia
    if fecha_actual.day < dia_fin:
        return datetime(fecha_actual.year, fecha_actual.month, dia_fin)
    if fecha_actual.month == 12:
        return datetime(fecha_actual.year + 1, 1, 15)
    else:
        return datetime(fecha_actual.year, fecha_actual.month + 1, 15)

# --- LÓGICA DE PAGOS (Capital Rey) ---
def generar_plan_pagos(prestamo, total_pagado):
    try:
        fecha_inicio = datetime.strptime(prestamo['fecha_inicio'], '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        fecha_inicio = datetime.strptime(prestamo['fecha_inicio'], '%Y-%m-%d %H:%M:%S')

    frecuencia = prestamo['frecuencia_pago']
    cuota_base = round(prestamo['monto_cuota'], 2)
    num_cuotas = prestamo['num_cuotas']
    
    total_deuda_registrada = round(prestamo['total_pagar'], 2)
    total_capital_teorico = round(cuota_base * num_cuotas, 2)
    mora_total_global = round(total_deuda_registrada - total_capital_teorico, 2)
    if mora_total_global < 0.05: mora_total_global = 0
    
    dinero_disponible = round(total_pagado, 2)
    plan = []
    
    if frecuencia == 'semanal':
        dia_semana = fecha_inicio.weekday()
        if dia_semana <= 2: dias_sabado = 5 - dia_semana
        else: dias_sabado = (5 - dia_semana) + 7
        proxima_fecha = fecha_inicio + timedelta(days=dias_sabado)
    elif frecuencia == 'quincenal':
        proxima_fecha = calcular_siguiente_quincena(fecha_inicio)
    else:
        proxima_fecha = fecha_inicio + timedelta(days=30)

    cuotas_pagadas_full = 0
    saldo_temp = dinero_disponible
    for i in range(1, num_cuotas + 1):
        if saldo_temp >= (cuota_base - 0.05):
            saldo_temp = round(saldo_temp - cuota_base, 2)
            cuotas_pagadas_full += 1
        else:
            break
            
    dinero_sobrante_para_mora = saldo_temp
    if mora_total_global > 0:
        mora_pagada = min(mora_total_global, dinero_sobrante_para_mora)
        mora_viva_visual = round(mora_total_global - mora_pagada, 2)
        dinero_sobrante_para_mora = round(dinero_sobrante_para_mora - mora_pagada, 2)
    else:
        mora_viva_visual = 0
        
    abono_parcial_capital = dinero_sobrante_para_mora
    mora_ya_pintada = False

    for i in range(1, num_cuotas + 1):
        monto_mostrar = cuota_base
        estado = "PENDIENTE"
        clase = "pending"
        detalle = ""
        es_pendiente_real = False # Flag para saber si esta es la "siguiente" a pagar
        
        if i <= cuotas_pagadas_full:
            estado = "PAGADO"
            clase = "paid"
        else:
            es_pendiente_real = True # Encontrada una no pagada
            if not mora_ya_pintada:
                monto_mostrar = round(cuota_base + mora_viva_visual, 2)
                if mora_viva_visual > 0.05:
                    detalle = f'<div style="color:#c0392b; font-weight:bold; font-size:0.85rem; margin-top:2px;">⚠️ + Mora: C${mora_viva_visual:,.2f}</div>'
                
                if abono_parcial_capital > 0.05:
                    falta = round(monto_mostrar - abono_parcial_capital, 2)
                    estado = f"PARCIAL (Resta C${falta:,.2f})"
                    if mora_viva_visual > 0.05: clase = "mora-row"
                    else: clase = "partial"
                    detalle += f'<div style="color:#16a085; font-size:0.8rem;">✓ Abonado: C${abono_parcial_capital:,.2f}</div>'
                else:
                    estado = "PENDIENTE"
                    if mora_viva_visual > 0.05: clase = "mora-row"
                    else: clase = "pending"
                mora_ya_pintada = True
            else:
                monto_mostrar = cuota_base
                estado = "PENDIENTE"
                clase = "pending"

        plan.append({
            'numero': i, 'fecha': proxima_fecha.strftime('%d/%m/%Y'), 'monto': monto_mostrar, 'estado': estado, 'clase': clase, 'detalle': detalle,
            'es_pendiente': es_pendiente_real, # Usaremos esto para el dashboard del cliente
            'tiene_mora': (mora_viva_visual > 0.05 and mora_ya_pintada and es_pendiente_real)
        })
        
        if frecuencia == 'semanal': proxima_fecha += timedelta(weeks=1)
        elif frecuencia == 'quincenal':
            if proxima_fecha.day <= 15:
                ud = obtener_ultimo_dia_mes(proxima_fecha.year, proxima_fecha.month)
                df = 30 if ud >= 30 else ud
                proxima_fecha = datetime(proxima_fecha.year, proxima_fecha.month, df)
            else:
                if proxima_fecha.month == 12: proxima_fecha = datetime(proxima_fecha.year + 1, 1, 15)
                else: proxima_fecha = datetime(proxima_fecha.year, proxima_fecha.month + 1, 15)
        else: proxima_fecha += timedelta(days=30)

    return plan

# --- PLANTILLA HTML ---

HTML_CABECERA = """
<!DOCTYPE html>
<html lang="es" data-theme="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>PrestamosApp Ni</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --primary: #0f3460; --accent: #e94560; --success: #27ae60; --danger: #c0392b; --warning: #f39c12; --info: #16213e; --bg: #f2f2f2; --card-bg: #ffffff; --text: #1a1a2e; --border: #dcdcdc; --muted: #666; --table-head: #e8e8e8; --table-row: #f9f9f9; }
        [data-theme="dark"] { --primary: #3282b8; --accent: #ff2e63; --success: #00b894; --danger: #ff7675; --warning: #ffeaa7; --info: #0f3460; --bg: #0f172a; --card-bg: #1e293b; --text: #e2e8f0; --border: #334155; --muted: #94a3b8; --table-head: #334155; --table-row: #1e293b; }
        * { box-sizing: border-box; } 
        body { font-family: 'Inter', sans-serif; background-color: var(--bg); color: var(--text); margin: 0; padding: 0; transition: background-color 0.3s, color 0.3s; }
        .navbar { background-color: var(--info); padding: 1rem; display: flex; justify-content: space-between; align-items: center; color: white; position: sticky; top: 0; z-index: 1000; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.2); }
        .navbar-brand { font-weight: 800; font-size: 1.2rem; display: flex; align-items: center; gap: 8px; letter-spacing: -0.5px; }
        .navbar-menu a { color: #e2e8f0; text-decoration: none; margin-left: 15px; font-size: 0.9rem; font-weight: 500; transition: color 0.2s; }
        .container { max-width: 1100px; margin: 1.5rem auto; padding: 0 1rem; padding-bottom: 80px; }
        .card { background: var(--card-bg); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 3px 6px rgba(0,0,0,0.08); border: 1px solid var(--border); transition: background-color 0.3s; }
        .card h3 { margin-top: 0; color: var(--primary); font-size: 1.1rem; border-bottom: 2px solid var(--border); padding-bottom: 10px; margin-bottom: 15px; font-weight: 700; }
        label { display: block; margin-bottom: 5px; font-size: 0.8rem; font-weight: 600; color: var(--muted); }
        input, select { width: 100%; padding: 10px; margin-bottom: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--card-bg); color: var(--text); font-size: 15px; }
        .btn { display: inline-flex; justify-content: center; align-items: center; padding: 10px 16px; border-radius: 6px; text-decoration: none; font-weight: 600; cursor: pointer; border: none; font-size: 0.9rem; transition: all 0.2s; gap: 5px; }
        .btn:hover { transform: translateY(-1px); filter: brightness(110%); }
        .btn-primary { background-color: var(--primary); color: white; }
        .btn-success { background-color: var(--success); color: white; }
        .btn-danger { background-color: var(--danger); color: white; }
        .btn-warning { background-color: var(--warning); color: #1a1a2e; } 
        .btn-info { background-color: var(--info); color: white; }
        .btn-whatsapp { background-color: #25D366; color: white; }
        .btn-outline { border: 1px solid var(--border); color: var(--text); background: var(--card-bg); }
        .btn-full { width: 100%; }
        .btn-sm { padding: 6px 10px; font-size: 0.75rem; }
        .search-bar { display: flex; gap: 8px; margin-bottom: 15px; }
        .table-wrapper { overflow-x: auto; max-height: 350px; overflow-y: auto; border-radius: 8px; border: 1px solid var(--border); }
        table { width: 100%; border-collapse: collapse; min-width: 100%; }
        thead th { position: sticky; top: 0; background-color: var(--table-head); z-index: 10; text-align: left; padding: 10px; font-size: 0.75rem; color: var(--text); font-weight: 700; text-transform: uppercase; border-bottom: 2px solid var(--border); }
        td { padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 0.85rem; color: var(--text); vertical-align: middle; }
        tbody tr:nth-child(even) { background-color: var(--table-row); }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; }
        .grid-4-dashboard { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }
        .stat-card { text-align: center; padding: 15px; color: white; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .stat-label { font-size: 0.75rem; opacity: 0.9; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; }
        .stat-number { font-size: 1.6rem; font-weight: 800; margin: 0; }
        .badge { padding: 4px 10px; border-radius: 12px; font-size: 0.7rem; font-weight: 700; }
        .paid { background: #d4edda; color: #155724; }
        .pending { background: #f8d7da; color: #721c24; }
        .partial { background: #fff3cd; color: #856404; }
        .mora-row { color: var(--danger); font-weight: bold; background: rgba(192, 57, 43, 0.1); border-left: 4px solid var(--danger); }
        .theme-toggle { position: fixed; bottom: 20px; right: 20px; background: var(--primary); color: white; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1.5rem; cursor: pointer; box-shadow: 0 4px 10px rgba(0,0,0,0.3); border: none; z-index: 2000; transition: transform 0.2s; }
        .theme-toggle:hover { transform: scale(1.1); }
        .greeting-anim { animation: fadeIn 2s ease-in-out; font-size: 1.2rem; font-weight: 300; color: #e2e8f0; margin-right: 15px; }
        @keyframes fadeIn { 0% { opacity: 0; transform: translateY(-10px); } 100% { opacity: 1; transform: translateY(0); } }
        /* CLIENTE SUMMARY CARD */
        .summary-card { background: linear-gradient(135deg, var(--primary) 0%, var(--info) 100%); color: white; padding: 25px; border-radius: 15px; margin-bottom: 25px; box-shadow: 0 5px 15px rgba(0,0,0,0.2); }
        .summary-title { font-size: 0.9rem; opacity: 0.9; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 5px; }
        .summary-amount { font-size: 2.5rem; font-weight: 800; margin: 0; }
        .summary-status { display: inline-block; padding: 6px 12px; background: rgba(255,255,255,0.2); border-radius: 20px; font-size: 0.85rem; font-weight: 600; margin-top: 10px; }
        .summary-date { font-size: 1.1rem; margin-top: 5px; opacity: 0.9; }
        .alert-mora { background: linear-gradient(135deg, #c0392b 0%, #e74c3c 100%); }
        .alert-ok { background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%); }

        @media (max-width: 768px) { .grid-2, .grid-3, .navbar, .grid-4-dashboard { grid-template-columns: 1fr; flex-direction: column; display: flex; } .navbar-menu { width: 100%; justify-content: space-around; margin-top: 10px; } .grid-3 { gap: 10px; } .greeting-anim { display: none; } }
        @media print { .no-print, .search-bar, .btn, .theme-toggle { display: none !important; } .card { border: none; shadow: none; padding: 0; margin-bottom: 0; background: white; color: black; } .hide-on-print { display: none !important; } body { background: white; color: black; font-size: 11pt; } .navbar { display: none; } .table-wrapper { max-height: none; overflow: visible; border: none; } .print-header { display: block !important; text-align: center; margin-bottom: 20px; border-bottom: 2px solid #000; padding-bottom: 10px; } .stat-card { color: black !important; background: white !important; border: 1px solid #ccc; } [data-theme="dark"] { --text: black; --bg: white; --card-bg: white; } }
        .print-header { display: none; }
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
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            {% for message in messages %}
                <div class="no-print" style="padding:10px; background:#2ecc71; color:white; border-radius:6px; margin-bottom:20px;">{{ message }}</div>
            {% endfor %}
        {% endif %}
    {% endwith %}
"""

HTML_PIE = """</div>
    <div class="no-print" style="text-align: center; padding: 20px; color: var(--muted); font-size: 0.8rem;">&copy; 2026 Sistema Nicaragua</div>
    <script>
        const htmlEl = document.documentElement;
        const btn = document.getElementById('themeBtn');
        if (localStorage.getItem('theme') === 'dark') { htmlEl.setAttribute('data-theme', 'dark'); btn.textContent = '☀️'; }
        function toggleTheme() {
            if (htmlEl.getAttribute('data-theme') === 'dark') { htmlEl.setAttribute('data-theme', 'light'); localStorage.setItem('theme', 'light'); btn.textContent = '🌙'; } 
            else { htmlEl.setAttribute('data-theme', 'dark'); localStorage.setItem('theme', 'dark'); btn.textContent = '☀️'; }
        }
        document.addEventListener('DOMContentLoaded', function() {
            const hour = new Date().getHours();
            const greetingEl = document.getElementById('greeting');
            let msg = '';
            if (hour >= 5 && hour < 12) { msg = 'Buenos días ☀️'; } else if (hour >= 12 && hour < 19) { msg = 'Buenas tardes 🌤️'; } else { msg = 'Buenas noches 🌙'; }
            if(greetingEl) greetingEl.textContent = msg;
        });
    </script>
</body>
</html>
"""

# --- RUTAS ---

@app.route('/')
def home():
    return render_template_string(HTML_CABECERA + """
    <div style="text-align: center; padding: 50px 0;">
        <h1 style="color: var(--primary); font-size: 2.5rem; margin-bottom: 10px;">Gestión de Préstamos</h1>
        <div style="display: flex; gap: 15px; justify-content: center; flex-wrap: wrap;">
            <a href='/admin_login' class="btn btn-primary btn-lg">Acceso Administrador</a>
            <a href='/cliente_login' class="btn btn-outline btn-lg">Acceso Cliente</a>
        </div>
    </div>""" + HTML_PIE)

@app.route('/admin_login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form['password'] == PASSWORD_ADMIN:
            session['admin_logged_in'] = True
            return redirect('/admin')
        else: flash('Contraseña incorrecta')
    return render_template_string(HTML_CABECERA + """
    <div class="card" style="max-width:400px; margin:40px auto; text-align:center;">
        <h3>🔒 Seguridad</h3>
        <form method="post"><input type="password" name="password" placeholder="Contraseña" required>
        <button type="submit" class="btn btn-primary btn-full">Ingresar</button></form>
    </div>""" + HTML_PIE)

@app.route('/logout')
def logout():
    session.pop('admin_logged_in', None)
    return redirect('/')

@app.route('/recibo/<int:pago_id>')
def descargar_recibo(pago_id):
    # Permitir a admin o cualquier usuario logueado (cliente también) descargar si tiene el link
    # En un sistema real verificaríamos que el recibo pertenezca al cliente logueado.
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute('''SELECT pg.id as pago_id, pg.monto, pg.fecha, c.nombre, c.cedula, p.id as prestamo_id 
                      FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id JOIN clientes c ON p.cliente_id = c.id WHERE pg.id = ?''', (pago_id,))
    data = cursor.fetchone(); conn.close()
    if not data: return redirect('/')
    pdf = ReciboPDF()
    pdf.add_page(); pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, f"Fecha: {data['fecha'][:19]}", 0, 1)
    pdf.cell(0, 10, f"Recibo #: {data['pago_id']:06d}", 0, 1)
    pdf.ln(10)
    pdf.set_font("Arial", 'B', 12); pdf.cell(40, 10, "Cliente:", 0, 0)
    pdf.set_font("Arial", '', 12); pdf.cell(0, 10, f"{data['nombre']} ({data['cedula']})", 0, 1)
    pdf.set_font("Arial", 'B', 12); pdf.cell(40, 10, "Prestamo ID:", 0, 0)
    pdf.set_font("Arial", '', 12); pdf.cell(0, 10, f"#{data['prestamo_id']}", 0, 1)
    pdf.ln(10); pdf.set_fill_color(240, 240, 240); pdf.set_font("Arial", 'B', 14)
    pdf.cell(0, 15, f"MONTO PAGADO: C$ {data['monto']:.2f}", 1, 1, 'C', fill=True)
    response = make_response(pdf.output(dest='S').encode('latin-1'))
    response.headers.set('Content-Disposition', 'attachment', filename=f'recibo_{data["pago_id"]}.pdf')
    response.headers.set('Content-Type', 'application/pdf')
    return response

@app.route('/descargar_backup')
def descargar_backup():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    try: return send_file('prestamos.db', as_attachment=True, download_name=f"backup_prestamos_{datetime.now().strftime('%Y%m%d')}.db")
    except: flash("Error al descargar backup"); return redirect('/admin')

@app.route('/admin')
def admin():
    if not session.get('admin_logged_in'): return redirect('/admin_login') 
    search_query = request.args.get('q', '')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM pagos WHERE prestamo_id NOT IN (SELECT id FROM prestamos)")
    cursor.execute("DELETE FROM cargos_extra WHERE prestamo_id NOT IN (SELECT id FROM prestamos)")
    cursor.execute("DELETE FROM prestamos WHERE cliente_id NOT IN (SELECT id FROM clientes)")
    conn.commit()
    
    if search_query: cursor.execute("SELECT * FROM clientes WHERE nombre LIKE ? ORDER BY nombre", ('%' + search_query + '%',))
    else: cursor.execute("SELECT * FROM clientes ORDER BY nombre")
    clientes = cursor.fetchall()
    
    cursor.execute("SELECT COALESCE(SUM(monto_original), 0) FROM prestamos")
    capital_colocado_total = cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(monto),0) FROM pagos")
    total_recaudado = cursor.fetchone()[0]
    
    cursor.execute('''SELECT COALESCE(SUM(pg.monto), 0) FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id WHERE p.aplica_seguro = 1''')
    recaudado_con_seguro = cursor.fetchone()[0]
    fondo_generado_acumulado = recaudado_con_seguro * 0.02
    
    cursor.execute('''SELECT pg.monto, p.monto_original, p.total_pagar, p.aplica_seguro, p.duracion_meses FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id''')
    todos_los_pagos = cursor.fetchall()
    ganancia_interes_acumulada = 0
    for p in todos_los_pagos:
        monto_pago = p[0]; capital = p[1]; total_deuda = p[2]; seguro_activo = p[3]; months = p[4]
        aporte_fondo = 0
        if seguro_activo == 1 and total_deuda > 0:
            seguro_total = capital * 0.02 * months
            aporte_fondo = seguro_total * (monto_pago / total_deuda)
        interes_total = total_deuda - capital
        ratio_interes = interes_total / total_deuda if total_deuda > 0 else 0
        interes_bruto_pago = monto_pago * ratio_interes
        ganancia_interes_acumulada += (interes_bruto_pago - aporte_fondo)

    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM retiros_fondo")
    total_retirado = cursor.fetchone()[0]
    fondo_disponible = fondo_generado_acumulado - total_retirado
    
    lista_reporte_clientes = []
    for cl in clientes:
        cursor.execute('''SELECT COALESCE(SUM(monto_original),0), COALESCE(SUM(total_pagar),0) FROM prestamos WHERE cliente_id=?''', (cl['id'],))
        res_prest = cursor.fetchone()
        prestado_hist = res_prest[0]
        total_acordado = res_prest[1]
        cursor.execute('''SELECT COALESCE(SUM(pg.monto),0) FROM pagos pg JOIN prestamos p ON pg.prestamo_id = p.id WHERE p.cliente_id=?''', (cl['id'],))
        pagado_hist = cursor.fetchone()[0]
        lista_reporte_clientes.append({'nombre': cl['nombre'], 'prestado': prestado_hist, 'pagado': pagado_hist, 'saldo': total_acordado - pagado_hist})

    conn.close()

    return render_template_string(HTML_CABECERA + """
    <div class="card" style="margin-bottom:20px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <h3>📊 Resumen Financiero</h3>
            <a href="/descargar_backup" class="btn btn-outline btn-sm">💾 Respaldar Datos</a>
        </div>
        <div style="height:200px;"><canvas id="financeChart"></canvas></div>
    </div>
    <script>
        const ctx = document.getElementById('financeChart').getContext('2d');
        new Chart(ctx, {
            type: 'bar',
            data: {
                labels: ['Capital Prestado', 'Total Recaudado', 'Ganancia Neta'],
                datasets: [{ label: 'Montos (C$)', data: [{{ capital_colocado_total }}, {{ total_recaudado }}, {{ ganancia_interes_acumulada }}], backgroundColor: ['#0f3460', '#27ae60', '#e94560'] }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    </script>
    <div class="grid-4-dashboard">
        <div class="stat-card" style="background:var(--primary);"><div class="stat-label">Capital Colocado</div><div class="stat-number">C${{ "{:,.2f}".format(capital_colocado_total) }}</div></div>
        <div class="stat-card" style="background:var(--success);"><div class="stat-label">Total Recaudado</div><div class="stat-number">C${{ "{:,.2f}".format(total_recaudado) }}</div></div>
        <div class="stat-card" style="background:#20bf6b;"><div class="stat-label">Ganancia Neta</div><div class="stat-number">C${{ "{:,.2f}".format(ganancia_interes_acumulada) }}</div></div>
        <div class="stat-card" style="background:var(--info);">
            <div class="stat-label">Fondo 2%</div><div class="stat-number">C${{ "{:,.2f}".format(fondo_disponible) }}</div>
            {% if fondo_disponible > 0.1 %}<form action="/vaciar_fondo" method="post" onsubmit="return confirm('¿Retirar?');"><input type="hidden" name="monto" value="{{ fondo_disponible }}"><button type="submit" class="btn btn-warning btn-sm" style="margin-top:8px; width:100%;">🗑️ Retirar</button></form>{% endif %}
        </div>
    </div>
    <div class="grid-2 main-layout hide-on-print">
        <div class="card" style="border-top: 4px solid var(--accent);">
            <h3>💰 Crear Préstamo</h3>
            <form action="/add_prestamo" method="post">
                <select name="cliente_id" required><option value="">Seleccionar Cliente...</option>{% for c in clientes_list %}<option value="{{ c['id'] }}">{{ c['nombre'] }}</option>{% endfor %}</select>
                <div class="grid-2"><div><label>Monto (C$)</label><input type="number" step="0.01" name="monto" required></div><div><label>Tasa (%)</label><input type="number" step="0.01" name="tasa" required></div></div>
                <div class="grid-2"><div><label>Fecha Inicio</label><input type="date" name="fecha_custom" style="font-size:0.8rem;"></div><div style="display:flex; align-items:center; margin-top:20px;"><input type="checkbox" name="seguro" value="1" style="width:20px; margin:0 10px 0 0;"><label style="margin:0; cursor:pointer;">¿Aplicar 2%?</label></div></div>
                <div class="grid-2"><div><label>Meses</label><input type="number" name="meses" required></div><div><label>Frecuencia</label><select name="frecuencia"><option value="semanal">Semanal</option><option value="quincenal">Quincenal</option><option value="mensual">Mensual</option></select></div></div>
                <button type="submit" class="btn btn-primary btn-full">Crear Préstamo</button>
            </form>
        </div>
        <div class="card">
            <h3>👥 Clientes</h3>
            <form action="/admin" method="get" class="search-bar no-print"><input type="text" name="q" placeholder="Buscar..." value="{{ query }}" style="margin:0;"><button type="submit" class="btn btn-primary">🔍</button>{% if query %}<a href="/admin" class="btn btn-outline">❌</a>{% endif %}</form>
            <form action="/add_cliente" method="post" class="hide-on-print" style="margin-bottom:15px; border-bottom:1px solid #eee; padding-bottom:15px;">
                <div class="grid-2"><input type="text" name="nombre" placeholder="Nombre" required style="margin-bottom:0;"><input type="text" name="telefono" placeholder="Teléfono" style="margin-bottom:0;"></div><input type="text" name="cedula" placeholder="Cédula" style="margin-top:10px; margin-bottom:0;"><button type="submit" class="btn btn-success btn-sm btn-full" style="margin-top:10px;">+ Nuevo Cliente</button>
            </form>
            <div class="table-wrapper">
                <table>
                    <thead><tr><th>Nombre</th><th>Teléfono</th><th>PIN</th><th class="no-print">Acción</th></tr></thead>
                    <tbody>
                    {% for c in clientes_list %}
                    <tr><td><div style="font-weight:600;">{{ c['nombre'] }}</div><div style="font-size:0.75rem; color:var(--muted);">{{ c['cedula'] }}</div></td><td>{{ c['telefono'] }}</td>
                    <td style="font-family:monospace; font-weight:bold; color:var(--accent);">{{ c['pin'] }}</td>
                    <td class="no-print" style="text-align:center; white-space:nowrap; width:1%;"><a href="/editar_cliente/{{ c['id'] }}" class="btn btn-outline btn-sm">✏️</a><a href="/admin/cliente/{{ c['id'] }}" class="btn btn-primary btn-sm">Ver</a></td></tr>
                    {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
    """ + HTML_PIE, clientes_list=clientes, query=search_query, 
    capital_colocado_total=capital_colocado_total, 
    total_recaudado=total_recaudado, 
    ganancia_interes_acumulada=ganancia_interes_acumulada, 
    fondo_disponible=fondo_disponible,
    reporte_cli=lista_reporte_clientes)

# --- RUTAS DE PROCESOS ---

@app.route('/vaciar_fondo', methods=['POST'])
def vaciar_fondo():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    monto = request.form['monto']
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO retiros_fondo (monto, fecha) VALUES (?, ?)", (monto, datetime.now()))
    conn.commit(); conn.close()
    flash(f'Fondo retirado (C${monto}).')
    return redirect('/admin')

@app.route('/editar_cliente/<int:id>', methods=['GET', 'POST'])
def editar_cliente(id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    if request.method == 'POST':
        cursor.execute("UPDATE clientes SET nombre=?, cedula=?, telefono=?, pin=? WHERE id=?", (request.form['nombre'], request.form['cedula'], request.form['telefono'], request.form['pin'], id))
        conn.commit(); conn.close(); flash('Actualizado'); return redirect('/admin')
    cursor.execute("SELECT * FROM clientes WHERE id=?", (id,)); cliente = cursor.fetchone(); conn.close()
    return render_template_string(HTML_CABECERA + """
    <div class="card" style="max-width:500px; margin:20px auto;">
        <h3>✏️ Editar Cliente</h3>
        <form method="post">
            <label>Nombre</label><input type="text" name="nombre" value="{{ c['nombre'] }}" required>
            <label>Cédula</label><input type="text" name="cedula" value="{{ c['cedula'] }}">
            <label>Teléfono</label><input type="text" name="telefono" value="{{ c['telefono'] }}">
            <label>PIN de Acceso</label><input type="text" name="pin" value="{{ c['pin'] }}" required>
            <div style="display:flex; gap:10px; margin-top:20px;">
                <a href="/admin" class="btn btn-outline btn-full">Cancelar</a>
                <button type="submit" class="btn btn-primary btn-full">Guardar</button>
            </div>
        </form>
    </div>""" + HTML_PIE, c=cliente)

@app.route('/admin/cliente/<int:cliente_id>')
def detalle_cliente(cliente_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("SELECT * FROM clientes WHERE id = ?", (cliente_id,)); cliente = cursor.fetchone()
    query = '''SELECT p.*, COALESCE(SUM(pg.monto), 0) as pagado
               FROM prestamos p LEFT JOIN pagos pg ON p.id = pg.prestamo_id
               WHERE p.cliente_id = ? GROUP BY p.id ORDER BY p.estado_prestamo ASC, p.id ASC'''
    cursor.execute(query, (cliente_id,)); prestamos = cursor.fetchall(); conn.close()

    contenido = """
    <div class="no-print" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:15px;">
        <a href="/admin" class="btn btn-outline btn-sm">← Volver</a>
        <div style="display:flex; gap:10px;">
            {% if cliente['telefono'] %}
                <a href="https://wa.me/505{{ cliente['telefono']|replace('-','')|replace(' ','') }}?text=Hola {{ cliente['nombre'] }}, le recordamos que tiene una cuota pendiente." target="_blank" class="btn btn-whatsapp btn-sm">📲 WhatsApp</a>
            {% endif %}
            <button onclick="window.print()" class="btn btn-primary btn-sm">🖨️ Ficha</button>
            <a href="/delete_cliente/{{ cliente['id'] }}" onclick="return confirm('¿SE BORRARÁ TODO EL HISTORIAL DE ESTE CLIENTE?')" class="btn btn-danger btn-sm">🗑️ Borrar Cliente</a>
        </div>
    </div>

    <div class="card">
        <h2 style="margin:0;">{{ cliente['nombre'] }}</h2>
        <small>Tel: {{ cliente['telefono'] }} | PIN: <strong>{{ cliente['pin'] }}</strong></small>
    </div>

    {% for p in prestamos %}
        {% set deuda = p['total_pagar'] - p['pagado'] %}
        <div class="card" style="border-left: 5px solid {% if p['estado_prestamo']=='REFINANCIADO' %}#95a5a6{% elif deuda < 1 %}var(--success){% else %}var(--warning){% endif %}; position:relative;">
            <a href="/delete_prestamo/{{ p['id'] }}" onclick="return confirm('¿Borrar SOLO este préstamo?')" class="no-print" style="position:absolute; top:15px; right:15px; text-decoration:none; font-size:1.2rem;">🗑️</a>
            <div style="margin-bottom:10px; padding-right:40px;">
                <strong>Préstamo #{{ loop.index }}</strong>
                {% if p['estado_prestamo']=='REFINANCIADO' %}<span class="badge" style="background:#cbd5e1; color:#333;">CERRADO</span>
                {% elif deuda < 1 %}<span class="badge paid">PAGADO</span>
                {% else %}<span class="badge pending">ACTIVO</span>{% endif %}
                {% if p['aplica_seguro'] == 1 %}<span class="badge" style="background:#e0cffc; color:#5b21b6;">2%</span>{% endif %}
            </div>
            
            <div class="grid-2" style="font-size:0.9rem;">
                <div>Deuda: <strong>C${{ "{:,.2f}".format(p['total_pagar']) }}</strong></div>
                <div>Abonado: <strong style="color:var(--success);">C${{ "{:,.2f}".format(p['pagado']) }}</strong></div>
                <div>Resta: <strong style="color:var(--danger);">C${{ "{:,.2f}".format(deuda) }}</strong></div>
            </div>

            {% if p['estado_prestamo'] == 'ACTIVO' and deuda > 0.1 %}
                <hr style="margin:15px 0; border-color:var(--border);" class="no-print">
                <div class="grid-2 no-print">
                    <form action="/aplicar_mora/{{ p['id'] }}" method="post" onsubmit="return confirm('¿Aplicar 3% de Mora?');">
                        <input type="hidden" name="monto_cuota" value="{{ p['monto_cuota'] }}">
                        <button type="submit" class="btn btn-warning btn-sm btn-full">⚠️ Mora (3%)</button>
                    </form>
                    <a href="/refinanciar/{{ p['id'] }}" onclick="return confirm('¿Refinanciar?')" class="btn btn-primary btn-sm btn-full">🔄 Refinanciar</a>
                </div>
                <form action="/add_pago" method="post" class="no-print" style="margin-top:10px; display:flex; gap:10px;">
                    <input type="hidden" name="prestamo_id" value="{{ p['id'] }}">
                    <input type="hidden" name="origen" value="detalle">
                    <input type="hidden" name="cliente_id" value="{{ cliente['id'] }}">
                    <input type="number" step="0.01" name="monto_pago" placeholder="Monto (C$)" required style="margin:0;">
                    <button type="submit" class="btn btn-success">Pagar</button>
                </form>
            {% endif %}
            
            <div style="margin-top:10px;" class="no-print">
                <a href="/ver_plan/{{ p['id'] }}" class="btn btn-outline btn-sm btn-full">📅 Tabla Pagos</a>
            </div>
        </div>
    {% endfor %}
    """
    return render_template_string(HTML_CABECERA + contenido + HTML_PIE, cliente=cliente, prestamos=prestamos)

@app.route('/aplicar_mora/<int:prestamo_id>', methods=['POST'])
def aplicar_mora(prestamo_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    monto_cuota = float(request.form['monto_cuota'])
    mora = round(monto_cuota * 0.03, 2)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("UPDATE prestamos SET total_pagar = total_pagar + ? WHERE id=?", (mora, prestamo_id))
    cursor.execute("INSERT INTO cargos_extra (prestamo_id, monto, motivo, fecha) VALUES (?, ?, ?, ?)", (prestamo_id, mora, "MORA 3%", datetime.now()))
    conn.commit(); conn.close()
    flash(f'Mora de C${mora:.2f} aplicada.'); return redirect(request.referrer)

@app.route('/refinanciar/<int:prestamo_id>')
def refinanciar(prestamo_id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute("SELECT * FROM prestamos WHERE id=?", (prestamo_id,)); p = cursor.fetchone()
    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE prestamo_id=?", (prestamo_id,)); pagado = cursor.fetchone()[0]
    saldo = p['total_pagar'] - pagado
    cursor.execute("UPDATE prestamos SET estado_prestamo = 'REFINANCIADO' WHERE id=?", (prestamo_id,)); conn.commit(); conn.close()
    return render_template_string(HTML_CABECERA + f"""
    <div class="card" style="max-width:500px; margin:20px auto; border-top: 5px solid var(--primary);">
        <h3>🔄 Refinanciar</h3>
        <p>Saldo pendiente: <strong>C${saldo:.2f}</strong></p>
        <form action="/add_prestamo" method="post">
            <input type="hidden" name="cliente_id" value="{p['cliente_id']}">
            <label>Nuevo Capital</label><input type="number" step="0.01" name="monto" value="{saldo:.2f}" readonly style="background:#eee;">
            <label>Nueva Tasa (%)</label><input type="number" step="0.01" name="tasa" required>
            <div style="margin-bottom:10px;"><input type="checkbox" name="seguro" value="1"> <label style="display:inline;">¿Aplica 2%?</label></div>
            <div class="grid-2">
                <div><label>Meses</label><input type="number" name="meses" required></div>
                <div><label>Frecuencia</label><select name="frecuencia"><option value="semanal">Semanal</option><option value="quincenal">Quincenal</option><option value="mensual">Mensual</option></select></div>
            </div>
            <button type="submit" class="btn btn-primary btn-full">Crear Nuevo Préstamo</button>
        </form>
    </div>""" + HTML_PIE)

@app.route('/delete_prestamo/<int:id>')
def delete_prestamo(id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM pagos WHERE prestamo_id=?", (id,))
    cursor.execute("DELETE FROM cargos_extra WHERE prestamo_id=?", (id,))
    cursor.execute("DELETE FROM prestamos WHERE id=?", (id,))
    conn.commit(); conn.close(); flash('Préstamo eliminado'); return redirect(request.referrer)

@app.route('/exportar_excel')
def exportar_excel():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    query = '''SELECT c.nombre, c.cedula, c.telefono, p.id, p.monto_original, p.total_pagar, p.fecha_inicio, p.estado_prestamo, COALESCE(SUM(pg.monto), 0) 
               FROM clientes c JOIN prestamos p ON c.id = p.cliente_id LEFT JOIN pagos pg ON p.id = pg.prestamo_id GROUP BY p.id ORDER BY c.nombre'''
    cursor.execute(query); filas = cursor.fetchall(); conn.close()
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(['Cliente', 'Cedula', 'Telefono', 'ID Prestamo', 'Monto Original', 'Total Deuda', 'Pagado', 'Saldo', 'Fecha', 'Estado'])
    for f in filas: writer.writerow([f[0], f[1], f[2], f[3], f[4], f[5], f[8], f[5]-f[8], f[6], f[7]])
    output.seek(0)
    return Response(output, mimetype="text/csv", headers={"Content-Disposition":"attachment;filename=reporte.csv"})

# --- CLIENTES ---
@app.route('/cliente_login')
def cliente_login():
    return render_template_string(HTML_CABECERA + """
    <div class="card" style="max-width: 400px; margin: 40px auto;">
        <div style="text-align:center;"><h3>👋 Acceso Clientes</h3></div>
        <form action="/cliente_dashboard" method="post">
            <label>Nombre Completo</label>
            <input type="text" name="nombre" placeholder="Ej: francisco flores" required>
            <label>PIN de Seguridad (4 dígitos)</label>
            <input type="password" name="pin" placeholder="****" required maxlength="4" style="letter-spacing: 5px; text-align:center;">
            <button type="submit" class="btn btn-primary btn-full">Consultar Estado</button>
        </form>
    </div>""" + HTML_PIE)

@app.route('/cliente_dashboard', methods=['POST'])
def cliente_dashboard():
    nombre = request.form['nombre'].strip()
    pin = request.form['pin'].strip()
    conn = get_db_connection(); cursor = conn.cursor()
    
    # Validacion con PIN
    cursor.execute("SELECT * FROM clientes WHERE nombre = ? AND pin = ?", (nombre, pin))
    cliente = cursor.fetchone()
    
    if not cliente: 
        flash('Credenciales incorrectas (Nombre o PIN)')
        return redirect('/cliente_login')
        
    cursor.execute('''SELECT p.*, COALESCE(SUM(pg.monto), 0) as pagado FROM prestamos p LEFT JOIN pagos pg ON p.id = pg.prestamo_id 
                 WHERE p.cliente_id = ? GROUP BY p.id ORDER BY p.id ASC''', (cliente['id'],))
    prestamos = cursor.fetchall(); conn.close()
    
    # --- LÓGICA DE RESUMEN INTELIGENTE ---
    resumen_alerta = None
    resumen_tipo = 'ok' # ok o danger
    
    for p in prestamos:
        pagado = p['pagado']
        if (p['total_pagar'] - pagado) > 0.5: # Si el prestamo esta activo
            plan = generar_plan_pagos(p, pagado)
            # Buscamos la primera cuota pendiente
            for cuota in plan:
                if cuota['es_pendiente']:
                    if cuota['tiene_mora']:
                        resumen_tipo = 'danger'
                        resumen_alerta = {'fecha': cuota['fecha'], 'monto': cuota['monto'], 'mensaje': '⚠️ TIENES UN ATRASO'}
                    else:
                        if not resumen_alerta: # Solo guardamos el primero que encontremos si no hay mora
                            resumen_alerta = {'fecha': cuota['fecha'], 'monto': cuota['monto'], 'mensaje': 'Próximo Pago'}
                    break 
        if resumen_tipo == 'danger': break # Si ya encontramos mora, esa es la prioridad
        
    return render_template_string(HTML_CABECERA + """
    <div style="display:flex; justify-content:space-between; align-items:center;">
        <h2>Hola, {{ nombre }}</h2> 
        <a href="/cliente_login" class="btn btn-outline btn-sm">Salir</a>
    </div>

    {% if alerta %}
    <div class="summary-card {% if tipo == 'danger' %}alert-mora{% else %}alert-ok{% endif %}">
        <div class="summary-title">{{ alerta.mensaje }}</div>
        <div class="summary-amount">C${{ "{:,.2f}".format(alerta.monto) }}</div>
        <div class="summary-date">Fecha límite: {{ alerta.fecha }}</div>
        {% if tipo == 'ok' %}<div class="summary-status">✅ ESTÁS AL DÍA</div>{% else %}<div class="summary-status">❌ PAGO REQUERIDO</div>{% endif %}
    </div>
    {% else %}
    <div class="summary-card alert-ok">
        <div class="summary-title">ESTADO DE CUENTA</div>
        <div class="summary-amount">C$0.00</div>
        <div class="summary-status">✨ ¡FELICIDADES! NO TIENES DEUDAS PENDIENTES</div>
    </div>
    {% endif %}

    {% for p in prestamos %}
        {% set deuda = p['total_pagar'] - p['pagado'] %}
        <div class="card" style="border-left: 5px solid {% if p['estado_prestamo']=='REFINANCIADO' %}#ccc{% elif deuda < 1 %}var(--success){% else %}var(--accent){% endif %};">
            <div style="display:flex; justify-content:space-between;"><strong>Préstamo #{{ loop.index }}</strong>{% if p['estado_prestamo']=='REFINANCIADO' %}<span class="badge">CERRADO</span>{% endif %}</div>
            <p>Deuda Total: C${{ "{:,.2f}".format(p['total_pagar']) }} <br> Pagado: C${{ "{:,.2f}".format(p['pagado']) }}</p>
            <p style="color:var(--danger)">Resta: C${{ "{:,.2f}".format(deuda) }}</p>
            <a href="/ver_plan/{{ p['id'] }}" class="btn btn-primary btn-sm btn-full">📅 Ver Calendario de Pagos</a>
        </div>
    {% else %}<div class="card">No tienes préstamos registrados.</div>{% endfor %}
    """ + HTML_PIE, nombre=cliente['nombre'], prestamos=prestamos, alerta=resumen_alerta, tipo=resumen_tipo)

@app.route('/add_cliente', methods=['POST'])
def add_cliente():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        pin_generado = str(random.randint(1000, 9999)) # Generar PIN
        cursor.execute("INSERT INTO clientes (nombre, cedula, telefono, pin) VALUES (?, ?, ?, ?)", (request.form['nombre'].strip(), request.form['cedula'], request.form['telefono'], pin_generado))
        conn.commit(); conn.close()
        flash(f'✅ Cliente guardado. SU PIN DE ACCESO ES: {pin_generado}') # Mostrar PIN al admin
    except: flash('Error: Nombre existente')
    return redirect('/admin')

@app.route('/add_prestamo', methods=['POST'])
def add_prestamo():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    monto = float(request.form['monto']); tasa = float(request.form['tasa']); meses = int(request.form['meses']); frecuencia = request.form['frecuencia']
    fecha_custom = request.form.get('fecha_custom')
    if fecha_custom:
        fecha_inicio_obj = datetime.strptime(fecha_custom, '%Y-%m-%d')
        fecha_inicio = fecha_inicio_obj.strftime('%Y-%m-%d %H:%M:%S.%f')
    else: fecha_inicio = datetime.now()
    aplica_seguro = 1 if request.form.get('seguro') else 0
    interes = monto * (tasa * meses / 100); total_bruto = monto + interes
    if frecuencia == 'semanal': cuotas = meses * 4
    elif frecuencia == 'quincenal': cuotas = meses * 2
    else: cuotas = meses
    valor_cuota_exacta = round(total_bruto / cuotas, 2)
    total_final_exacto = round(valor_cuota_exacta * cuotas, 2)
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute('''INSERT INTO prestamos (cliente_id, monto_original, tasa_mensual, duracion_meses, frecuencia_pago, num_cuotas, monto_cuota, total_pagar, fecha_inicio, estado_prestamo, aplica_seguro) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVO', ?)''', 
                 (request.form['cliente_id'], monto, tasa, meses, frecuencia, cuotas, valor_cuota_exacta, total_final_exacto, fecha_inicio, aplica_seguro))
    conn.commit(); conn.close(); flash('Préstamo creado')
    return redirect('/admin')

@app.route('/add_pago', methods=['POST'])
def add_pago():
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("INSERT INTO pagos (prestamo_id, monto, fecha) VALUES (?, ?, ?)", (request.form['prestamo_id'], request.form['monto_pago'], datetime.now()))
    conn.commit(); conn.close(); flash('Pago registrado')
    if request.form.get('origen') == 'detalle': return redirect(f"/admin/cliente/{request.form['cliente_id']}")
    return redirect('/admin')

@app.route('/delete_cliente/<int:id>')
def delete_cliente(id):
    if not session.get('admin_logged_in'): return redirect('/admin_login')
    conn = get_db_connection(); cursor = conn.cursor()
    cursor.execute("DELETE FROM pagos WHERE prestamo_id IN (SELECT id FROM prestamos WHERE cliente_id=?)", (id,))
    cursor.execute("DELETE FROM cargos_extra WHERE prestamo_id IN (SELECT id FROM prestamos WHERE cliente_id=?)", (id,))
    cursor.execute("DELETE FROM prestamos WHERE cliente_id=?", (id,))
    cursor.execute("DELETE FROM clientes WHERE id=?", (id,))
    conn.commit(); conn.close(); flash('Cliente eliminado completamente')
    return redirect('/admin')

@app.route('/ver_plan/<int:prestamo_id>')
def ver_plan(prestamo_id):
    conn = get_db_connection(); conn.row_factory = sqlite3.Row; cursor = conn.cursor()
    cursor.execute('''SELECT p.*, c.nombre FROM prestamos p JOIN clientes c ON p.cliente_id = c.id WHERE p.id=?''', (prestamo_id,))
    p = cursor.fetchone()
    cursor.execute("SELECT * FROM pagos WHERE prestamo_id=? ORDER BY fecha DESC", (prestamo_id,)); historial = cursor.fetchall()
    cursor.execute("SELECT * FROM cargos_extra WHERE prestamo_id=? ORDER BY fecha DESC", (prestamo_id,)); cargos = cursor.fetchall()
    movimientos = []
    for h in historial: movimientos.append({'id': h['id'], 'fecha': h['fecha'], 'monto': h['monto'], 'tipo': 'ABONO', 'color': 'green'})
    for cargo in cargos: movimientos.append({'id': None, 'fecha': cargo['fecha'], 'monto': cargo['monto'], 'tipo': cargo['motivo'], 'color': 'red'})
    movimientos.sort(key=lambda x: x['fecha'], reverse=True)
    cursor.execute("SELECT COALESCE(SUM(monto), 0) FROM pagos WHERE prestamo_id=?", (prestamo_id,)); pagado = cursor.fetchone()[0]
    conn.close()
    plan = generar_plan_pagos(p, pagado)
    return render_template_string(HTML_CABECERA + """
    <div class="no-print" style="margin-bottom:15px; display:flex; justify-content:space-between;">
        <a href="javascript:history.back()" class="btn btn-outline btn-sm">← Volver</a>
        <button onclick="window.print()" class="btn btn-primary btn-sm">🖨️ Imprimir / Descargar PDF</button>
    </div>
    <div class="print-header"><h2>Tabla de Amortización</h2><p><strong>Cliente:</strong> {{ p['nombre'] }}</p><p>Generado: <script>document.write(new Date().toLocaleDateString())</script></p></div>
    <div class="card">
        <h3 class="no-print">Plan de Pagos: {{ p['nombre'] }}</h3>
        <div style="background:var(--card-bg); padding:15px; border-radius:10px; margin-bottom:20px; font-size:0.9rem; border:1px solid var(--border);">
            <strong>Monto Original:</strong> C${{ "{:,.2f}".format(p['monto_original']) }}<br>
            <strong>Total a Pagar:</strong> C${{ "{:,.2f}".format(p['total_pagar']) }}<br>
            <strong>{{ p['num_cuotas'] }} Cuotas</strong> de <strong>C${{ "{:,.2f}".format(p['monto_cuota']) }}</strong> ({{ p['frecuencia_pago'] }})
        </div>
        <h4 style="margin-bottom:10px;">Proyección de Cuotas</h4>
        <div class="table-wrapper"><table>
        <thead><tr><th>#</th><th>Fecha</th><th>Monto</th><th>Estado</th></tr></thead>
        <tbody>{% for f in plan %}<tr>
            <td>{{ f['numero'] }}</td><td>{{ f['fecha'] }}</td>
            <td>C${{ "{:,.2f}".format(f['monto']) }}{% if f['detalle'] %}{{ f['detalle']|safe }}{% endif %}</td>
            <td><span class="badge {{ f['clase'] }}">{{ f['estado'] }}</span></td>
        </tr>{% endfor %}</tbody>
        </table></div>
        {% if movs %}
        <h4 style="margin-top:30px; margin-bottom:10px; color:var(--primary); border-top:1px solid var(--border); padding-top:15px;">Historial de Movimientos</h4>
        <div class="table-wrapper"><table>
        <thead><tr><th>Fecha Real</th><th>Concepto</th><th>Monto</th><th class="no-print">Acción</th></tr></thead>
        <tbody>{% for h in movs %}<tr>
            <td>{{ h['fecha'][:19] }}</td><td>{{ h['tipo'] }}</td>
            <td style="color:{{ h['color'] }}; font-weight:bold;">{% if h['color'] == 'red' %}+{% else %}-{% endif %} C${{ "{:,.2f}".format(h['monto']) }}</td>
            <td class="no-print">{% if h['tipo'] == 'ABONO' %}<a href="/recibo/{{ h['id'] }}" class="btn btn-info btn-sm" style="padding:2px 8px; font-size:0.7rem;">📄 Recibo</a>{% endif %}</td>
        </tr>{% endfor %}</tbody>
        </table></div>
        {% endif %}
    </div>""" + HTML_PIE, p=p, plan=plan, movs=movimientos)

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
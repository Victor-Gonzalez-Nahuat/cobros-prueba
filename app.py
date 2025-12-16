from flask import Flask, abort, jsonify, render_template, redirect, request
import stripe
import os
from dotenv import load_dotenv
import mysql.connector

# Cargar variables de entorno
load_dotenv()

app = Flask(__name__)

# Configuración de Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# --- Funciones de Base de Datos ---

def get_db_connection():
    """Establece y retorna la conexión a MySQL con manejo de errores."""
    try:
        # Intentamos conectar a la BD usando variables de entorno
        return mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            # Puerto fijo por consistencia con tu configuración original
            port=51558 
        )
    except mysql.connector.Error as err:
        # Si la conexión falla, imprimimos el error y retornamos None
        print(f"ERROR: Fallo al conectar a la base de datos: {err}")
        return None

# --- Rutas Web ---

@app.route("/")
def index():
    """Ruta principal, redirige a la búsqueda de predios."""
    return redirect("/buscar-predio")

@app.route("/buscar-predio", methods=["GET", "POST"])
def buscar_predio():
    """
    Muestra el formulario de búsqueda (GET) y procesa la búsqueda por folio (POST).
    Renderiza los resultados en una tabla.
    """
    predios_encontrados = None
    
    if request.method == "POST":
        folio_buscado = request.form.get("folio")
        
        if folio_buscado:
            conn = get_db_connection()
            if conn is None:
                return "Error interno del servidor: Fallo al conectar con la base de datos.", 500

            cursor = conn.cursor(dictionary=True)
            try:
                # Consulta para buscar predios que coincidan con el folio
                # Usamos LIKE %s para permitir búsqueda parcial o fuzziness
                query = "SELECT id, folio, monto, estado FROM PREDIOS_PRUEB WHERE folio LIKE %s"
                cursor.execute(query, (f"%{folio_buscado}%",)) 
                predios_encontrados = cursor.fetchall()
            except mysql.connector.Error as err:
                print(f"Error al ejecutar la consulta SQL: {err}")
                predios_encontrados = [] # Retorna lista vacía si falla la consulta
            finally:
                cursor.close()
                conn.close()

    # Renderiza la plantilla buscar_predio.html
    return render_template(
        "buscar_predio.html",
        predios=predios_encontrados,
        stripe_key=os.getenv("STRIPE_PUBLIC_KEY")
    )


@app.post("/crear-checkout")
def crear_checkout():
    """Crea una nueva sesión de Stripe Checkout y redirige al usuario."""
    try:
        predio_id = int(request.form["predio_id"])
    except (TypeError, ValueError):
        return "ID de predio inválido", 400

    # 1. Obtener predio de la BD para verificar el monto (Seguridad)
    conn = get_db_connection()
    if conn is None:
        return "Error interno del servidor", 500
        
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM PREDIOS_PRUEB WHERE id = %s", (predio_id,))
        predio = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if not predio:
        return "Predio no encontrado", 404
        
    # El monto debe estar en centavos para Stripe, y se toma de la BD
    try:
        monto = int(predio["monto"] * 100)
    except (TypeError, ValueError):
        return "Monto de predio inválido en la base de datos.", 500

    # 2. Crear la Sesión de Checkout en Stripe
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "mxn",
                "product_data": {
                    "name": f"Pago predio {predio['folio']}"
                },
                "unit_amount": monto,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url="http://127.0.0.1:5000/exito",
        cancel_url="http://127.0.0.1:5000/cancelado",
        metadata={
            "predio_id": predio_id
        }
    )

    # 3. Redirigir al usuario a la URL de pago de Stripe
    return redirect(session.url, code=303)


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Endpoint para recibir eventos de Stripe (Webhooks)."""
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    event = None

    # 1. Verificar la firma del Webhook
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        # Payload inválido
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        # Firma inválida
        return "Invalid signature", 400

    # 2. Manejar el evento (Solo nos interesa 'checkout.session.completed')
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        
        # Validar metadata antes de usarla
        if 'predio_id' not in session['metadata']:
             print("Advertencia: metadata 'predio_id' no encontrada en la sesión completada.")
             return jsonify({'status': 'metadata_missing'}), 200 # No es un error crítico para Stripe

        try:
            predio_id = int(session['metadata']['predio_id'])
        except (TypeError, ValueError):
            print(f"Advertencia: predio_id inválido en metadata: {session['metadata']['predio_id']}")
            return jsonify({'status': 'invalid_predio_id'}), 200 # No es un error crítico para Stripe

        # 3. Actualizar estado en la BD (transacción crítica)
        conn = get_db_connection()
        if conn is None:
            # Si falla la conexión aquí, Stripe reintentará el webhook
            return "DB Connection Error", 500 

        cursor = conn.cursor()
        try:
            # Añadimos la condición 'AND estado != "pagado"' para manejar la idempotencia
            cursor.execute(
                "UPDATE PREDIOS_PRUEB SET estado = 'pagado' WHERE id = %s AND estado != 'pagado'",
                (predio_id,)
            )
            conn.commit()
            print(f"✅ Predio ID {predio_id} marcado como pagado. Filas afectadas: {cursor.rowcount}")
        except mysql.connector.Error as err:
            print(f"ERROR: Fallo al actualizar la BD en webhook: {err}")
            conn.rollback()
            # Devolvemos un 500 para que Stripe reintente el webhook más tarde
            return "Database Update Failed", 500
        finally:
            cursor.close()
            conn.close()

    # 4. Responder a Stripe que el evento fue recibido exitosamente
    return jsonify({'status': 'success'}), 200

@app.route("/exito")
def exito():
    """Página de éxito de pago."""
    return "✅ Pago exitoso. ¡Gracias por su contribución!"

@app.route("/cancelado")
def cancelado():
    """Página de cancelación de pago."""
    return "❌ Pago cancelado. Puede intentarlo de nuevo desde la página de búsqueda."

if __name__ == "__main__":
    # Usa el puerto de la variable de entorno PORT (para Railway) o 5000 por defecto
    port = int(os.environ.get("PORT", 5000))  
    app.run(host="0.0.0.0", port=port, debug=True) # debug=True para desarrollo
from flask import Flask, abort, jsonify, render_template, redirect, request
import stripe
import os
from dotenv import load_dotenv
import mysql.connector

load_dotenv()

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

# Conexión a MySQL
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=51558
    )

@app.route("/")
def index():
    return render_template(
        "pago.html",
        stripe_key=os.getenv("STRIPE_PUBLIC_KEY")
    )

@app.route("/predios")
def listar_predios():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM PREDIOS_PRUEB")
    predios = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template(
        "predios.html",
        predios=predios
    )

@app.post("/crear-checkout")
def crear_checkout():
    predio_id = int(request.form["predio_id"])

    # Obtener predio de la BD
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM PREDIOS_PRUEB WHERE id = %s", (predio_id,))
    predio = cursor.fetchone()
    cursor.close()
    conn.close()

    if not predio:
        return "Predio no encontrado", 404

    monto = int(predio["monto"] * 100)

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

    return redirect(session.url, code=303)

@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        predio_id = int(session['metadata']['predio_id'])

        # Actualizar estado en la BD
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE PREDIOS_PRUEB SET estado = 'pagado' WHERE id = %s",
            (predio_id,)
        )
        conn.commit()
        cursor.close()
        conn.close()

        print(f"✅ Predio ID {predio_id} marcado como pagado")

    return jsonify({'status': 'success'})

@app.route("/exito")
def exito():
    return "✅ Pago exitoso"

@app.route("/cancelado")
def cancelado():
    return "❌ Pago cancelado"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))  # usa el PORT que da Railway, si no, 5000
    app.run(host="0.0.0.0", port=port)


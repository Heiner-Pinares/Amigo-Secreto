# Amigo secreto de Año Nuevo

Aplicación web autocontenida para registrar participantes, importar un CSV, configurar restricciones, generar una vista previa, confirmar el sorteo en una transacción y controlar envíos idempotentes.

## Tecnología y almacenamiento

- Python 3.11 o posterior, únicamente con la biblioteca estándar.
- Servidor WSGI local.
- SQLite para eventos, participantes, restricciones, vistas previas, asignaciones, estados de correo y auditoría técnica.
- HTML/CSS/JavaScript sin dependencias externas.
- Sesión administrativa, autorización y tokens CSRF en todas las acciones POST.

La carpeta estaba vacía al iniciar, por lo que no había framework, almacenamiento, autenticación, integración de correo ni componentes previos que reutilizar.

## Inicio rápido (sin enviar correos)

1. Copia `.env.example` como `.env`.
2. Cambia `SECRET_GIFT_ADMIN_PASSWORD` por una contraseña local robusta.
3. Desde esta carpeta, ejecuta:

   ```bash
   python3 app.py
   ```

4. Abre `http://127.0.0.1:8000`.
5. Usa `http://127.0.0.1:8000/admin` para administrar el evento.

La base de datos se crea en `data/secret_gift.sqlite3`. La carpeta `data/` y `.env` están ignorados por Git.

El envío real está deliberadamente desactivado en `app.py` hasta implementar el adaptador de la función de correo. Esto permite registrar, importar, generar la vista previa y confirmar el sorteo sin riesgo de enviar mensajes.

## Formulario e importación

El formulario público solicita nombre completo, correo, regalos o preferencias, área opcional y confirmación de participación. El servidor normaliza espacios y correos, valida los campos y rechaza correos duplicados.

Para importar:

1. Entra al panel administrativo.
2. Selecciona un CSV o pega su contenido.
3. Usa columnas `nombre`, `correo`, `regalos`; opcionalmente `area` y `confirmado`.
4. Pulsa **Revisar importación**.
5. Revisa filas válidas, duplicadas e inválidas.
6. Pulsa **Importar filas válidas**.

La importación es aditiva e idempotente por correo: no borra participantes existentes y no vuelve a insertar correos ya registrados. También reconoce encabezados equivalentes en español o inglés y delimitadores coma, punto y coma o tabulación. Microsoft Forms puede exportarse primero a CSV desde su Excel/SharePoint; no se usa automatización visual del navegador.

## Preparar y ejecutar el sorteo

1. Revisa o excluye participantes mientras las inscripciones están abiertas.
2. Configura fecha, hora, lugar, presupuesto e instrucciones.
3. Agrega bloqueos direccionales entre parejas y, si corresponde, activa **Evitar asignaciones dentro de la misma área**.
4. Pulsa **Generar vista previa del sorteo**.
5. La aplicación calcula y valida la combinación completa antes de almacenarla como vista previa. Si las restricciones son imposibles, no guarda ninguna asignación.
6. Puedes cancelar la vista previa para volver a abrir la edición.
7. Pulsa **Confirmar sorteo** y acepta la confirmación explícita.

La confirmación guarda todas las asignaciones en una única transacción. Una recarga o reinicio no cambia el resultado confirmado. La interfaz no muestra la lista completa de pares; únicamente presenta resúmenes y, tras el envío, el estado de cada participante remitente.

## Correo y reintentos

El contrato interno está en `secret_gift/mail.py` y entrega al adaptador:

```json
{
  "to": "participante@empresa.com",
  "subject": "Tu amigo secreto — Intercambio de Año Nuevo",
  "html": "<html>...</html>",
  "text": "Versión en texto plano...",
  "idempotency_key": "identificador-del-sorteo:identificador-del-participante"
}
```

Cada asignación conserva una clave única y uno de estos estados:

- `pending`: pendiente.
- `accepted`: aceptado por la función de correo; no garantiza entrega al buzón.
- `failed`: error seguro para reintentar manualmente.
- `unknown`: timeout o respuesta incierta; no se reintenta automáticamente para evitar duplicados.

El botón principal procesa solo pendientes. **Reintentar solo fallidos** nunca procesa aceptados ni inciertos. Un bloqueo en memoria y la transición transaccional a `sending` evitan ejecuciones simultáneas dentro del proceso. El proveedor debe respetar la clave de idempotencia para cubrir también reintentos entre procesos.

### Datos necesarios para conectar la función real

No se inventó ningún endpoint, autenticación o formato. Para implementar el adaptador faltan:

1. URL o mecanismo de ejecución.
2. Método HTTP o forma de invocación.
3. Tipo de autenticación.
4. Encabezados necesarios.
5. JSON o parámetros de entrada.
6. Ejemplo de respuesta exitosa.
7. Ejemplo de respuesta de error.
8. Dirección y nombre del remitente autorizados.
9. Confirmación de compatibilidad con HTML y texto plano.
10. Límites de envío, timeout y política de reintentos.
11. Confirmación de si acepta una clave de idempotencia.

Las credenciales se leerán exclusivamente desde variables de entorno y la llamada se hará en el backend. Nunca deben aparecer en HTML, JavaScript, logs, respuestas del navegador ni archivos versionados.

## Pruebas

Ejecuta:

```bash
python3 -m unittest -v
```

Las pruebas usan bases temporales y un servicio de correo simulado. Cubren sorteos de 2, 3, 10 y 100 personas; unicidad; autoasignación; participantes insuficientes; restricciones imposibles; duplicados; importación; rollback; autorización; CSRF; secretos no expuestos; vista previa sin envío; doble clic; reintentos; timeout; e inyección HTML.

## Seguridad operativa

- No subas `.env`, la base SQLite, tokens, contraseñas ni datos reales al repositorio.
- Ejecuta detrás de HTTPS y un proxy/servidor WSGI endurecido si se publica fuera de un equipo local.
- Limita el acceso a `/admin` y rota la contraseña ante cualquier sospecha.
- Realiza copias de seguridad cifradas de la base antes del sorteo real.
- Los logs de auditoría guardan identificadores, acción, estado y error técnico; no guardan preferencias ni pares completos.
- Para desactivar temporalmente el correo, conserva `SECRET_GIFT_MAIL_ENABLED=false` y el adaptador en modo deshabilitado.

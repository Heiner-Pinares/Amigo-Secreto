# Amigo secreto de Año Nuevo

Portal privado para que un único administrador cargue participantes desde Excel, confirme un sorteo completo y envíe los resultados mediante el mecanismo corporativo SSH → `sendmail`.

## Arquitectura

- Python 3.11 o posterior, Paramiko para SSH y openpyxl para leer archivos `.xlsx`.
- Servidor WSGI local y SQLite.
- HTML/CSS/JavaScript sin frameworks de navegador y sin formulario público de inscripción.
- Sesiones administrativas, autorización y CSRF en todas las acciones POST.
- Servicio `SshSendmailService` desacoplado en `secret_gift/mail.py`.
- Sorteo guardado transaccionalmente antes de cualquier envío.

La base SQLite conserva participantes, restricciones, vista previa, asignaciones, prueba de correo realizada, claves de idempotencia y estados de envío. La lista completa de asignaciones no se expone públicamente.

## Instalación en Windows

Desde PowerShell, dentro del proyecto:

```powershell
py -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Configura una contraseña administrativa robusta. Para probar la aplicación con el correo deshabilitado:

```env
SECRET_GIFT_ADMIN_PASSWORD=cambia-esta-contrasena
SECRET_GIFT_EMAIL_ENABLED=false
```

Ejecuta:

```powershell
py -m unittest -v
py app.py
```

Abre `http://127.0.0.1:8000`. La raíz redirige al acceso privado `/admin`; no existe registro público.

## Cargar participantes desde Excel

El administrador selecciona directamente un archivo `.xlsx` en el panel. El libro debe contener una hoja con estas columnas, aunque se aceptan nombres equivalentes:

- `Nombre completo`
- `Correo` o `Correo electrónico`
- `Regalos` o `Preferencias`
- `Área`, `Equipo` o `Gerencia` (opcional)
- `Confirmado` (opcional)

La aplicación localiza los encabezados dentro de las primeras diez filas, normaliza espacios y correos, detecta duplicados y presenta errores antes de guardar. Solo se acepta la lista cuando contiene al menos dos participantes y ninguna fila inválida.

Al confirmar la importación, la nueva lista reemplaza la lista anterior antes de preparar el sorteo. El archivo está limitado a 5 MB y su contenido descomprimido a 25 MB.

## Preparar el sorteo

1. Inicia sesión en el portal privado.
2. Carga el Excel y pulsa **Usar esta lista de participantes**.
3. Revisa participantes, preferencias y áreas.
4. Configura fecha, hora, lugar, presupuesto e instrucciones.
5. Agrega restricciones opcionales si son necesarias.
6. Pulsa **Generar vista previa del sorteo**.
7. La aplicación valida la asignación completa sin mostrar los pares ni enviar correos.
8. Pulsa **Confirmar sorteo**.

Cada persona regala y recibe exactamente una vez, nadie se asigna a sí mismo y no existen destinatarios repetidos. Confirmar guarda todas las asignaciones en una transacción; enviar correos nunca vuelve a generar el sorteo.

## Configuración SSH/sendmail

El transporte es exclusivamente:

```text
Aplicación → SSH → 172.19.30.62 → sendmail → destinatario
```

Edita `.env`:

```env
SECRET_GIFT_EMAIL_ENABLED=false
SECRET_GIFT_SSH_HOST=172.19.30.62
SECRET_GIFT_SSH_PORT=22
SECRET_GIFT_SSH_USER=usr_mds
SECRET_GIFT_SSH_PASSWORD_FILE=secrets/secret_gift_ssh_password.txt
SECRET_GIFT_SSH_KNOWN_HOSTS=secrets/secret_gift_known_hosts
SECRET_GIFT_EMAIL_FROM=remitente-autorizado@claro.com.pe
SECRET_GIFT_EMAIL_FROM_NAME=Amigo secreto de Año Nuevo
SECRET_GIFT_EMAIL_TIMEOUT=30
SECRET_GIFT_EMAIL_MAX_PER_RUN=100
SECRET_GIFT_TEST_MODE=true
SECRET_GIFT_TEST_RECIPIENT=correo-controlado@claro.com.pe
```

No existe una variable de contraseña. La ruta no se interpreta como contraseña.

### Archivo privado de contraseña

Crea manualmente:

```powershell
New-Item -ItemType Directory -Force secrets
notepad secrets\secret_gift_ssh_password.txt
```

El archivo debe contener solamente la contraseña en una línea. No agregues comillas ni el nombre de la variable. En Windows, limita el acceso mediante ACL al usuario que ejecuta el portal y a administradores. Ejemplo, sustituyendo `CUENTA_SERVICIO` por la cuenta real:

```powershell
icacls secrets\secret_gift_ssh_password.txt /inheritance:r
icacls secrets\secret_gift_ssh_password.txt /grant:r "CUENTA_SERVICIO:R" "Administrators:F"
```

En Linux:

```bash
chmod 600 secrets/secret_gift_ssh_password.txt
```

`secrets/`, `.env` y `data/` están excluidos de Git y de imágenes Docker mediante `.gitignore` y `.dockerignore`.

### Clave conocida del servidor

El equipo responsable debe proporcionar o verificar por un canal confiable la entrada de `known_hosts` correspondiente a `172.19.30.62`. Guárdala en:

```text
secrets/secret_gift_known_hosts
```

No se usa `AutoAddPolicy`. Una clave ausente, desconocida o modificada cancela la conexión. No aceptes una huella obtenida de la misma conexión sin compararla con la huella oficial.

## Secuencia segura de activación

1. Conserva `SECRET_GIFT_EMAIL_ENABLED=false` mientras preparas los archivos privados.
2. Completa remitente, destinatario de prueba, contraseña y `known_hosts`.
3. Cambia `SECRET_GIFT_EMAIL_ENABLED=true` y conserva `SECRET_GIFT_TEST_MODE=true`.
4. Reinicia la aplicación.
5. En `/admin`, pulsa **Probar conexión SSH**. Esta acción no envía correo.
6. Pulsa **Enviar correo de prueba** y confirma que llegó al buzón autorizado.
7. La aplicación registra esa prueba satisfactoria en SQLite.

Mientras `SECRET_GIFT_TEST_MODE=true`, todos los mensajes de la cola se sustituyen por `SECRET_GIFT_TEST_RECIPIENT`. El asunto indica el destinatario original. Esto permite revisar el contenido sin escribir a participantes reales.

Solo después de validar la prueba puedes cambiar:

```env
SECRET_GIFT_TEST_MODE=false
```

Reinicia la aplicación. La pantalla mostrará una advertencia de **modo real activo**. Antes de procesar pendientes o fallidos debes escribir exactamente `ENVIAR CORREOS`.

## Estados e idempotencia

- `pending`: pendiente.
- `sending`: reservado por el proceso actual.
- `accepted`: `sendmail` devolvió código 0; no garantiza entrega en Outlook.
- `failed`: fallo confirmado que admite reintento manual.
- `unknown`: resultado remoto incierto; no se reintenta automáticamente.

La clave se compone de evento, sorteo y participante. La cola selecciona solo pendientes o, en la acción separada, fallidos. El bloqueo de proceso y la transición previa a `sending` evitan el doble clic y el envío concurrente. Un proceso interrumpido deja el mensaje en estado incierto de operación (`sending`) en vez de reenviarlo automáticamente.

Los logs de auditoría guardan identificadores, administrador, fecha, duración, código de salida, estado y tipo de error. No guardan contraseña, regalos, MIME completo, comando remoto ni la lista de direcciones.

## Pruebas

```powershell
py -m unittest -v
```

Las pruebas simulan Paramiko y nunca abren una conexión real. Cubren importación y reemplazo desde Excel; ausencia de registro público; conexión SSH exitosa; contraseña ausente o vacía; `known_hosts` ausente; clave desconocida o cambiada; autenticación rechazada; timeout; código no cero de `sendmail`; límite de `stderr`; destinatarios inválidos e inyección; modo de prueba; MIME HTML/texto; idempotencia; reintento de fallidos; permisos administrativos; CSRF; rollback y validez del sorteo.

## Secretos que nunca deben versionarse

- `.env`
- `secrets/secret_gift_ssh_password.txt`
- `secrets/secret_gift_known_hosts`
- La base SQLite de producción
- Datos reales de participantes

Para desactivar inmediatamente cualquier envío, cambia `SECRET_GIFT_EMAIL_ENABLED=false` y reinicia la aplicación.

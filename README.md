# Workshop: Hermes + Gemma 4 en Google Cloud

Este repo sirve para levantar Hermes en una VM de Google Cloud y usar `Gemma 4` desde `Vertex AI`.

La idea es simple:

1. Creas un proyecto en Google Cloud.
2. Activas Vertex y Gemma 4.
3. Creas una VM pequeña.
4. Clonas este repo.
5. Ejecutas un script.
6. Conectas Hermes al proxy local.

## Qué monta este repo

- `Hermes Agent` en la VM
- un proxy local compatible con OpenAI en `127.0.0.1:8080`
- `Vertex AI` como backend real de inferencia

## Requisitos

- Un proyecto de Google Cloud con billing
- Una VM de Compute Engine
- Acceso SSH a la VM
- Vertex AI activado
- `Gemma 4 26B A4B IT API Service` activado en Model Garden
- La VM debe usar una `service account`
- Esa `service account` debe tener permisos para usar Vertex AI
- En la VM, en `Access scopes`, selecciona `Allow full access to all Cloud APIs`

VM recomendada:

- `4 vCPU`
- `8 GB RAM`

Importante:

- No basta con crear la VM y ya.
- Si la VM no tiene `Allow full access to all Cloud APIs`, el proxy podrá arrancar pero Vertex devolverá errores de permisos o de scopes.
- En Google Cloud Console esto se configura al editar la VM, en la sección de `Service account` y `Access scopes`.

## Paso 1: crear el proyecto en Google Cloud

En la consola de Google Cloud:

1. Abre `IAM & Admin` → `Manage resources`
2. Pulsa `Create Project`
3. Ponle un nombre
4. Entra en el proyecto nuevo
5. Asegúrate de que `Billing` está activado

Qué estás haciendo aquí:

- crear el contenedor donde vivirán la VM, Vertex AI y los permisos

## Paso 2: activar las APIs necesarias

En la consola:

1. Ve a `APIs & Services` → `Enabled APIs & services`
2. Pulsa `Enable APIs and Services`
3. Activa `Vertex AI API`
4. Activa `Compute Engine API`

Qué estás haciendo aquí:

- habilitar Vertex AI para inferencia
- habilitar Compute Engine para crear la VM

## Paso 3: activar Gemma 4 en Vertex

En la consola:

1. Ve a `Vertex AI`
2. Abre `Model Garden`
3. Busca `Gemma 4 26B A4B IT API Service`
4. Entra en esa ficha
5. Pulsa `Enable` si aparece

Si quieres probarlo antes de tocar la VM, usa `Cloud Shell`:

```bash
# guarda el project id activo de gcloud en una variable
PROJECT_ID="$(gcloud config get-value project)"

# fija la región global usada por este modelo
REGION="global"

# fija el host de Vertex AI
ENDPOINT="aiplatform.googleapis.com"

# hace una llamada directa a Vertex para verificar acceso al modelo
curl \
  -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://${ENDPOINT}/v1/projects/${PROJECT_ID}/locations/${REGION}/endpoints/openapi/chat/completions" \
  -d '{
    "model": "google/gemma-4-26b-a4b-it-maas",
    "stream": false,
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: Vertex Gemma 4 is working."
      }
    ]
  }'
```

Qué debe pasar:

- Vertex debe devolver una respuesta JSON válida
- el texto debe decir `Vertex Gemma 4 is working.`

## Paso 4: crear la VM

En la consola:

1. Ve a `Compute Engine` → `VM instances`
2. Pulsa `Create instance`
3. Elige una máquina de `4 vCPU` y `8 GB RAM`
4. Usa una imagen Ubuntu o Debian reciente
5. En `Service account`, deja una cuenta válida del proyecto
6. En `Access scopes`, selecciona `Allow full access to all Cloud APIs`
7. Crea la VM

Qué estás haciendo aquí:

- crear la máquina donde corre Hermes
- dar a la VM scopes suficientes para que el proxy pueda hablar con Vertex

## Paso 5: entrar por SSH y clonar el repo

Ejecuta esto dentro de la VM:

```bash
# actualiza el índice de paquetes de la VM
sudo apt update

# instala git para poder clonar el repo
sudo apt install -y git

# clona este repo en una carpeta local
git clone https://github.com/VSBDev/GCPHermesWorkshop.git

# entra en la carpeta del proyecto
cd GCPHermesWorkshop

# ejecuta el instalador principal y le pasa tu project id
bash scripts/bootstrap-vm.sh --project-id TU_PROJECT_ID

# recarga el PATH de la shell actual por si Hermes acaba de instalarse
source ~/.bashrc

# comprueba que la shell ya encuentra Hermes
command -v hermes

# comprueba que Hermes responde correctamente
hermes --version
```

Qué hace este bloque:

- prepara la VM
- descarga este repo
- instala Hermes si hace falta, pero sin lanzar el setup interactivo dentro del instalador
- crea el proxy local
- levanta el servicio `vertex-openai-proxy`

Importante:

- no uses `--skip-hermes` salvo que Hermes ya exista de verdad en esa VM
- si al terminar `hermes` no aparece, ejecuta `source ~/.bashrc`

## Paso 6: validar que Hermes y el proxy están listos

```bash
# verifica que la shell encuentra el binario de Hermes
command -v hermes

# verifica que Hermes responde
hermes --version

# muestra el estado del servicio systemd del proxy
systemctl status vertex-openai-proxy.service --no-pager

# verifica que el proxy está respondiendo localmente
curl http://127.0.0.1:8080/healthz

# verifica qué modelo está exponiendo el proxy a Hermes
curl http://127.0.0.1:8080/v1/models
```

Qué debe pasar:

- `command -v hermes` debe devolver una ruta
- `hermes --version` debe funcionar
- el servicio debe aparecer como `active (running)`
- `/healthz` debe devolver tu `project_id`
- `/v1/models` debe devolver `google/gemma-4-26b-a4b-it-maas`

## Paso 7: conectar Hermes

```bash
# abre la configuración de modelo de Hermes
hermes model
```

Dentro de Hermes elige:

- `Custom endpoint`
- URL: `http://127.0.0.1:8080/v1`
- API key: vacío
- Model: `google/gemma-4-26b-a4b-it-maas`
- Context window: `256000`

Luego arranca Hermes:

```bash
# inicia Hermes ya apuntando al proxy local
hermes
```

## Paso 8: probar Vertex desde Hermes

```text
Reply with exactly: Hermes is using Vertex Gemma 4.
```

Si responde, ya está funcionando.

## Paso 9: conectar Telegram

Primero crea un bot con `@BotFather` y saca tu user id con `@userinfobot`.

Luego en la VM:

```bash
# abre el asistente de configuración del gateway
hermes gateway setup
```

Cuando pregunte:

- selecciona `Telegram`
- pega el `bot token`
- pega tu `Telegram user ID`

Después instala el servicio:

```bash
# instala el gateway como servicio del sistema
sudo "$(command -v hermes)" gateway install --system --run-as-user "$USER"

# arranca el gateway de Telegram
sudo "$(command -v hermes)" gateway start --system

# comprueba que el gateway está corriendo
sudo systemctl status hermes-gateway.service --no-pager
```

Qué hace este bloque:

- registra Hermes Gateway como servicio
- lo deja arrancando aunque cierres SSH
- permite hablar con Hermes desde Telegram

## Explicación rápida del script principal

El archivo importante es:

- [scripts/bootstrap-vm.sh](/Users/victor/Desktop/HermesGemma4/scripts/bootstrap-vm.sh)

Hace esto, en este orden:

1. instala paquetes del sistema
2. instala Hermes si no existe
3. crea un virtualenv para el proxy
4. instala dependencias Python
5. escribe `proxy.env`
6. copia `vertex_openai_proxy.py`
7. crea el servicio `systemd`
8. arranca el proxy y lo verifica

## Archivos importantes

- [README.md](/Users/victor/Desktop/HermesGemma4/README.md)
- [scripts/bootstrap-vm.sh](/Users/victor/Desktop/HermesGemma4/scripts/bootstrap-vm.sh)
- [vertex_openai_proxy.py](/Users/victor/Desktop/HermesGemma4/vertex_openai_proxy.py)
- [requirements-proxy.txt](/Users/victor/Desktop/HermesGemma4/requirements-proxy.txt)

## Si algo falla

```bash
# revisa el log del proxy de Vertex
journalctl -u vertex-openai-proxy.service -n 100 --no-pager

# revisa el log del gateway de Telegram
journalctl -u hermes-gateway -n 100 --no-pager
```

Con esos dos logs normalmente encuentras el problema.

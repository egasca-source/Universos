# =============================================================================
# VERSION: api_universo_120526_03 (API - Separación de Privilegios)
# =============================================================================

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import pandas as pd

from app_universo import (
    ejecutar_proceso_reporte, estado_del_motor, 
    arrancar_motor_bi, extraer_esquema_parametros
)

app = FastAPI(title="Self-Service BI API")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

class SolicitudUpdate(BaseModel):
    usuario_activo: str
    respuestas_prompt: dict = {}

def verificar_estado_o_rechazar():
    """
    Función de Muro: Si un usuario pide algo y el servidor no está listo, 
    lo rechaza educadamente SIN encender nada.
    """
    estado_ram = estado_del_motor()
    
    if estado_ram["estado"] == "APAGADO":
        raise HTTPException(
            status_code=503, 
            detail={"estado": "APAGADO", "mensaje": "Servicio temporalmente inactivo. Espere a que el sistema sea inicializado por el administrador."}
        )
    elif estado_ram["estado"] == "CARGANDO":
        raise HTTPException(
            status_code=503, 
            detail={"estado": "CARGANDO", "mensaje": "El Universo se está conectando. Por favor, espere."}
        )
    return True

# =============================================================================
# 🛠️ PLANO DE CONTROL (Exclusivo para Automatizaciones / Administradores)
# =============================================================================

@app.post("/api/v1/sistema/iniciar_motor")
def endpoint_arranque_controlado(background_tasks: BackgroundTasks):
    """Interruptor maestro. Solo este endpoint tiene el poder de encender el motor."""
    estado_ram = estado_del_motor()
    if estado_ram["estado"] == "EN_LINEA":
        return {"estado": "OK", "mensaje": "El motor ya está operando normalmente."}
    if estado_ram["estado"] == "CARGANDO":
        return {"estado": "OK", "mensaje": "El motor ya se está inicializando."}
        
    background_tasks.add_task(arrancar_motor_bi)
    return {"estado": "OK", "mensaje": "Señal de arranque recibida. Cargando la RAM en segundo plano..."}

# =============================================================================
# 🖥️ PLANO DE DATOS (Consumo para Next.js / Usuarios Web)
# =============================================================================

@app.get("/health")
def revisar_salud_servidor():
    """Informa el estado para que Next.js dibuje la pantalla de carga si es necesario."""
    estado_ram = estado_del_motor()
    if estado_ram["estado"] == "APAGADO":
        raise HTTPException(status_code=503, detail="Motor BI apagado.")
    return {"estado": estado_ram["estado"], "detalle": estado_ram}

@app.get("/api/v1/reporte/{id_reporte}/parametros")
def obtener_parametros(id_reporte: int):
    verificar_estado_o_rechazar()
    
    esquema, error = extraer_esquema_parametros(id_reporte)
    if error == "MOTOR_NO_LISTO":
        raise HTTPException(status_code=503, detail={"estado": "CARGANDO"})
    elif error:
        raise HTTPException(status_code=500, detail=error)
        
    return {"estado": "EXITO", "esquema_interfaz": esquema}

@app.get("/api/v1/reporte/{id_reporte}/estado")
def obtener_estado(id_reporte: int, usuario: str):
    verificar_estado_o_rechazar()
    path_cache = f"datos_olap_rep{id_reporte}_{usuario}.parquet"
    return {"existe_cache": os.path.exists(path_cache)}

@app.post("/api/v1/reporte/{id_reporte}/actualizar")
def actualizar(id_reporte: int, req: SolicitudUpdate):
    verificar_estado_o_rechazar()
    res, error = ejecutar_proceso_reporte(id_reporte, req.usuario_activo, req.respuestas_prompt)
    if error: raise HTTPException(status_code=500, detail=error)
    return {"estado": "EXITO", "info": res}

@app.get("/api/v1/reporte/{id_reporte}/derivado/kpis")
def obtener_kpis(id_reporte: int, usuario: str):
    # Endpoint súper ligero. Lee disco, no toca el motor.
    path_cache = f"datos_olap_rep{id_reporte}_{usuario}.parquet"
    if not os.path.exists(path_cache): raise HTTPException(status_code=404)
    df = pd.read_parquet(path_cache)
    return {"filas": len(df)}

if __name__ == "__main__":
    import uvicorn
    import nest_asyncio
    nest_asyncio.apply()
    uvicorn.run(app, host="0.0.0.0", port=8000)
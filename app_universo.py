# =============================================================================
# VERSION: app_universo_120526_03 (Módulo de Servicio - Control Estricto)
# =============================================================================

import json
import ast
import os
import pandas as pd
from core_universo import inicializar_motor_bi
from motor_queries import generar_reporte

# --- MÁQUINA DE ESTADOS (Aislada de los usuarios) ---
ESTADO_SISTEMA = "APAGADO"  
POOL_UNIVERSOS = None

def arrancar_motor_bi():
    """ÚNICA función autorizada para cargar la RAM. Ejecutada por Admin."""
    global POOL_UNIVERSOS, ESTADO_SISTEMA
    
    if ESTADO_SISTEMA in ["CARGANDO", "EN_LINEA"]:
        return

    ESTADO_SISTEMA = "CARGANDO"
    print("\n>>> [APP] Iniciando carga controlada del Motor BI en memoria...")
    
    try:
        POOL_UNIVERSOS = inicializar_motor_bi(archivo_maestro='UNIVERSE_MASTER_REGISTRY.xlsx')
        ESTADO_SISTEMA = "EN_LINEA"
        print(">>> [APP] ¡Motor BI cargado y listo para operar!\n")
    except Exception as e:
        ESTADO_SISTEMA = "APAGADO"
        print(f">>> [APP] ERROR CRÍTICO al cargar motor: {e}\n")

def estado_del_motor():
    return {
        "estado": ESTADO_SISTEMA, 
        "universos_cargados": len(POOL_UNIVERSOS) if POOL_UNIVERSOS else 0
    }

def leer_excel_seguro(valor_celda, valor_por_defecto):
    if pd.isna(valor_celda) or str(valor_celda).strip().lower() in ['nan', 'none', '']: 
        return valor_por_defecto
    try: return ast.literal_eval(str(valor_celda).strip())
    except: return valor_por_defecto

def hidratar_arbol(nodo, respuestas):
    if isinstance(nodo, dict):
        if "valor" in nodo and isinstance(nodo["valor"], str):
            if str(nodo["valor"]).startswith("@"):
                nodo["valor"] = respuestas.get(nodo["valor"], nodo["valor"])
        for llave in nodo: hidratar_arbol(nodo[llave], respuestas)
    elif isinstance(nodo, list):
        for item in nodo: hidratar_arbol(item, respuestas)
    return nodo

def extraer_esquema_parametros(id_reporte):
    """Extrae metadata de filtros desde la RAM para el Front-End."""
    if ESTADO_SISTEMA != "EN_LINEA":
        return None, "MOTOR_NO_LISTO"
        
    try:
        df_reportes = pd.read_excel('REPORTES_SIMULADOS.xlsx', sheet_name='REPORTES')
        df_filtrado = df_reportes[df_reportes['ID_REPORTE'] == id_reporte]
        if df_filtrado.empty: return None, "Reporte no encontrado"
        
        reporte_row = df_filtrado.iloc[0]
        arbol_plantilla = leer_excel_seguro(reporte_row.get('ARBOL_FILTROS'), {})
        payload_consulta = leer_excel_seguro(reporte_row.get('PAYLOAD_CONSULTA'), [])
        
        universo_usado = payload_consulta[0].split('[')[0].strip().upper() if payload_consulta else None
        mapa_meta = POOL_UNIVERSOS.get(universo_usado, {}).get('mapa_metadatos', {}) if universo_usado else {}

        esquema_front_end = {}

        def cazar_prompts(nodo):
            if isinstance(nodo, dict):
                if "valor" in nodo and str(nodo["valor"]).startswith("@"):
                    prompt_id = nodo["valor"] 
                    # DESPUÉS: Busca cualquiera de las palabras que pudiste haber usado en tu JSON
                    variable_asociada = nodo.get("campo", nodo.get("variable", nodo.get("columna", "")))
                    meta_var = mapa_meta.get(variable_asociada, {})
                    
                    esquema_front_end[prompt_id] = {
                        "variable_origen": variable_asociada,
                        "operador": nodo.get("operador", "="),
                        "tipo_dato_ui": meta_var.get("tipo_dato_ui", "text"),
                        "tipo_dato_sql": meta_var.get("tipo_sql_nativo", "VARCHAR")
                    }
                for k, v in nodo.items(): cazar_prompts(v)
            elif isinstance(nodo, list):
                for item in nodo: cazar_prompts(item)

        cazar_prompts(arbol_plantilla)
        return esquema_front_end, None
    except Exception as e:
        return None, str(e)

def ejecutar_proceso_reporte(id_reporte, usuario, respuestas_prompt):
    """Orquestador principal. Falla por seguridad si el motor no está En Línea."""
    if ESTADO_SISTEMA != "EN_LINEA":
        return None, "MOTOR_NO_LISTO"
        
    try:
        df_reportes = pd.read_excel('REPORTES_SIMULADOS.xlsx', sheet_name='REPORTES')
        df_filtrado = df_reportes[df_reportes['ID_REPORTE'] == id_reporte]
        if df_filtrado.empty: return None, "Reporte no encontrado"
        
        reporte_row = df_filtrado.iloc[0]
        payload_consulta = leer_excel_seguro(reporte_row.get('PAYLOAD_CONSULTA'), [])
        payload_filtro = leer_excel_seguro(reporte_row.get('PAYLOAD_FILTRO'), [])
        arbol_plantilla = leer_excel_seguro(reporte_row.get('ARBOL_FILTROS'), {})
        
        arbol_hidratado = hidratar_arbol(arbol_plantilla.copy(), respuestas_prompt)
        
        df_resultado, sql_o_error = generar_reporte(
            pool_universos=POOL_UNIVERSOS, 
            payload_consulta=payload_consulta,
            payload_filtro=payload_filtro, 
            arbol_filtros=arbol_hidratado
        )
        
        if df_resultado is not None:
            path_parquet = f"datos_olap_rep{id_reporte}_{usuario}.parquet"
            path_csv = f"visualizacion_desarrollo_{id_reporte}.csv"
            df_resultado.to_parquet(path_parquet, engine='pyarrow', index=False)
            df_resultado.to_csv(path_csv, index=False, encoding='utf-8-sig')
            return {"archivo_cache": path_parquet, "filas": len(df_resultado), "sql_auditoria": sql_o_error}, None
        
        return None, sql_o_error
    except Exception as e: 
        return None, str(e)
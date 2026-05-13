import pandas as pd
import itertools
from sqlalchemy import func, and_, or_
from sqlalchemy.orm import sessionmaker
import traceback

def aplicar_operador(col_izq, col_der, op):
    op = str(op).strip()
    if op in ['=', '==']: return col_izq == col_der
    elif op in ['<>', '!=']: return col_izq != col_der
    elif op == '>': return col_izq > col_der
    elif op == '<': return col_izq < col_der
    elif op == '>=': return col_izq >= col_der
    elif op == '<=': return col_izq <= col_der
    return col_izq == col_der

def unificar_variables_solicitadas(vars_consulta, vars_filtro):
    clean_consulta = list(dict.fromkeys(vars_consulta))
    clean_filtro = list(dict.fromkeys(vars_filtro))
    data = [{'variable': v, 'procedencia': 'Consulta'} for v in clean_consulta]
    data.extend([{'variable': v, 'procedencia': 'Filtro'} for v in clean_filtro])
    return pd.DataFrame(data)

def obtener_rutas_catalogo(df_solicitud, mapa_metadatos):
    resultados = []
    for index, row_sol in df_solicitud.iterrows():
        ruta_enviada = str(row_sol['variable']).strip()
        procedencia = row_sol['procedencia']
        meta = mapa_metadatos.get(ruta_enviada)
        if not meta:
            resultados.append((ruta_enviada, "ADVERTENCIA: No encontrada", "", "", "", procedencia, None))
        else:
            resultados.append((meta['nombre_var'], ruta_enviada, meta['dependencias'], meta['tipo_dato'], meta['agregacion'], procedencia, meta.get('orm_obj')))
    return resultados

def buscar_relaciones_entre_dependencias(lista_metadatos, df_relations):
    tablas_unicas = set()
    for tupla in lista_metadatos:
        deps_raw = str(tupla[2]).strip()
        if deps_raw and deps_raw.lower() not in ["", "nan", "n/a"]:
            for parte in deps_raw.split(','):
                t = parte.strip()
                if t: tablas_unicas.add(t)
    
    pares_candidatos = list(itertools.permutations(tablas_unicas, 2))
    relaciones_encontradas = []
    
    for tabla_izq, tabla_der in pares_candidatos:
        coincidencias = df_relations[(df_relations['parent_class'] == tabla_izq) & (df_relations['child_class'] == tabla_der)]
        if not coincidencias.empty:
            for _, row in coincidencias.iterrows():
                relacion = (
                    tabla_izq, 
                    tabla_der, 
                    str(row['parent_cols']).strip(), 
                    str(row['child_cols']).strip(),
                    str(row.get('operadores', '==')).strip()
                )
                relaciones_encontradas.append(relacion)
    return relaciones_encontradas

def aplicar_filtros_anidados(query, arbol_filtros, lista_metadatos):
    if not arbol_filtros: return query
    mapa_objetos = {}
    mapa_tipos = {} 
    
    for nombre, ruta, deps, tipo, agg, proc, obj in lista_metadatos:
        if proc == 'Filtro' and obj is not None:
            mapa_objetos[ruta] = obj 
            mapa_tipos[ruta] = tipo
    
    def procesar_nodo(nodo):
        if 'logic' in nodo:
            operador = nodo['logic'].upper()
            hijos = [procesar_nodo(h) for h in nodo.get('conditions', [])]
            hijos_validos = [h for h in hijos if h is not None]
            if not hijos_validos: return None
            return and_(*hijos_validos) if operador == 'AND' else or_(*hijos_validos)
        elif 'variable' in nodo:
            nom_var = nodo['variable'] 
            if nom_var not in mapa_objetos: return None
            col = mapa_objetos[nom_var]
            if mapa_tipos.get(nom_var) == 'Filter': return col
            
            op = nodo.get('operador', 'Igual a')
            val = nodo.get('valor')
            try:
                if op in ['Igual a', '==']: return col == val
                elif op == 'Diferente de': return col != val
                elif op == 'En la Lista': return col.in_(val) if isinstance(val, list) else col == val
                elif op in ['Fuera De La Lista', 'Excepto']: return ~col.in_(val) if isinstance(val, list) else col != val
                elif op in ['Entre', 'Ambos']: return col.between(val[0], val[1])
                elif op == 'No Entre': return ~col.between(val[0], val[1])
                elif op == 'Mayor que': return col > val
                elif op == 'Mayor que o igual a': return col >= val
                elif op == 'Menor que': return col < val
                elif op == 'Menor que o igual a': return col <= val
                elif op == 'Corresponde al criterio': return col.like(f"{val}")
                elif op == 'Diferente del criterio': return ~col.like(f"{val}")
                elif op == 'Es Nulo': return col.is_(None)
                elif op == 'No es Nulo': return col.isnot(None)
                else: return col == val
            except Exception: return None
        return None

    condicion_maestra = procesar_nodo(arbol_filtros)
    if condicion_maestra is not None: query = query.filter(condicion_maestra)
    return query

def generar_reporte(pool_universos, payload_consulta, payload_filtro, arbol_filtros):
    if not payload_consulta: raise ValueError("El payload de consulta está vacío.")

    # Grupo 3 (Instancia 5): El front-end manda minúsculas ("smt['...']")
    primera_variable = payload_consulta[0]
    universo_payload = primera_variable.split('[')[0].strip()

    # Grupo 3 (Instancia 6): Buscamos en el Pool RAM usando MAYÚSCULAS
    universo_ram_key = universo_payload.upper()
    
    contexto = pool_universos.get(universo_ram_key)
    if not contexto: 
        raise ValueError(f"El universo '{universo_ram_key}' no se encuentra cargado en memoria.")

    print(f"\n[SERVER] Procesando petición para Universo '{universo_ram_key}'...")

    df_solicitud = unificar_variables_solicitadas(payload_consulta, payload_filtro)
    lista_metadatos = obtener_rutas_catalogo(df_solicitud, contexto['mapa_metadatos'])
    listado_joins = buscar_relaciones_entre_dependencias(lista_metadatos, contexto['df_relations'])

    Session = sessionmaker(bind=contexto['engine'])
    session = Session()

    try:
        campos_base = []
        tablas_requeridas = set()
        tablas_medidas = set()
        
        for nombre, ruta, deps, tipo, agg, proc, obj in lista_metadatos:
            if proc == 'Consulta' and obj is not None:
                campos_base.append(obj)
                
                if deps and str(deps).lower() not in ['nan', 'none', '']:
                    for t in str(deps).split(','):
                        t_clean = t.strip()
                        if t_clean in contexto['orm_classes']:
                            tablas_requeridas.add(t_clean)
                            if tipo == 'Measure': tablas_medidas.add(t_clean)

        if not tablas_requeridas: raise ValueError("No se encontraron dependencias válidas.")

        if tablas_medidas:
            TABLA_BASE_DINAMICA = sorted(list(tablas_medidas))[0]
        else:
            conteo_hijos = {t: 0 for t in tablas_requeridas}
            for padre, hijo, col_p, col_h, op in listado_joins:
                if hijo in conteo_hijos: conteo_hijos[hijo] += 1
            if conteo_hijos:
                TABLA_BASE_DINAMICA = sorted(conteo_hijos.keys(), key=lambda k: conteo_hijos[k], reverse=True)[0]
            else:
                TABLA_BASE_DINAMICA = sorted(list(tablas_requeridas))[0]

        query_base = session.query(*campos_base).select_from(contexto['orm_classes'][TABLA_BASE_DINAMICA])

        if len(tablas_requeridas) > 1:
            tablas_en_query = {TABLA_BASE_DINAMICA}
            agregados = True
            
            while agregados:
                agregados = False
                for padre, hijo, col_p_raw, col_h_raw, ops_raw in listado_joins:
                    if padre in tablas_requeridas and hijo in tablas_requeridas:
                        p_cols = col_p_raw.split(',')
                        c_cols = col_h_raw.split(',')
                        ops = ops_raw.split(',')
                        if len(ops) < len(p_cols): ops.extend(['=='] * (len(p_cols) - len(ops)))

                        if padre in tablas_en_query and hijo not in tablas_en_query:
                            t_padre, t_hijo = contexto['orm_classes'][padre], contexto['orm_classes'][hijo]
                            cond_list = []
                            for p, c, op in zip(p_cols, c_cols, ops):
                                cond_list.append(aplicar_operador(getattr(t_padre, p.strip()), getattr(t_hijo, c.strip()), op))
                            query_base = query_base.outerjoin(t_hijo, and_(*cond_list))
                            tablas_en_query.add(hijo); agregados = True
                            
                        elif hijo in tablas_en_query and padre not in tablas_en_query:
                            t_padre, t_hijo = contexto['orm_classes'][padre], contexto['orm_classes'][hijo]
                            cond_list = []
                            for p, c, op in zip(p_cols, c_cols, ops):
                                cond_list.append(aplicar_operador(getattr(t_padre, p.strip()), getattr(t_hijo, c.strip()), op))
                            query_base = query_base.outerjoin(t_padre, and_(*cond_list))
                            tablas_en_query.add(padre); agregados = True
            
            tablas_huerfanas = tablas_requeridas - tablas_en_query
            for huerfana in tablas_huerfanas:
                query_base = query_base.select_from(contexto['orm_classes'][huerfana])
                tablas_en_query.add(huerfana)

        query_base = aplicar_filtros_anidados(query_base, arbol_filtros, lista_metadatos)
        subq = query_base.subquery()

        campos_finales, campos_groupby = [], []
        tiene_measures = False
        
        for nombre, ruta, deps, tipo, agg, proc, obj in lista_metadatos:
            if proc == 'Consulta' and obj is not None:
                col_subq = subq.c[nombre] 
                if tipo == 'Measure':
                    tiene_measures = True
                    if agg == 'Suma': campos_finales.append(func.sum(col_subq).label(nombre))
                    elif agg == 'Conteo': campos_finales.append(func.count(col_subq).label(nombre))
                    elif agg == 'Promedio': campos_finales.append(func.avg(col_subq).label(nombre))
                    elif agg == 'Min': campos_finales.append(func.min(col_subq).label(nombre))
                    elif agg == 'Max': campos_finales.append(func.max(col_subq).label(nombre))
                    else: campos_finales.append(func.sum(col_subq).label(nombre))
                else:
                    campos_finales.append(col_subq)
                    campos_groupby.append(col_subq)

        query_final = session.query(*campos_finales).select_from(subq)
        if tiene_measures and campos_groupby: query_final = query_final.group_by(*campos_groupby)
        elif not tiene_measures: query_final = query_final.distinct()

        sql_generado = str(query_final.statement.compile(dialect=contexto['engine'].dialect, compile_kwargs={"literal_binds": True}))
        df_resultado = pd.read_sql(query_final.statement, session.bind)
        
        return df_resultado, sql_generado

    except Exception as e:
        print(f"\n[!] ERROR EN EJECUCIÓN: {e}")
        traceback.print_exc()
        return None, str(e)
    finally:
        session.close()
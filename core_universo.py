from sqlalchemy import create_engine, MetaData, Table, distinct, Column, Integer, String, func, select, and_, or_, Case, literal, Date, text, not_, literal_column
from sqlalchemy.orm import sessionmaker, relationship, configure_mappers
from sqlalchemy.ext.declarative import declarative_base
import pyodbc
import pandas as pd
import graphviz
import os
import re
import traceback 
import itertools
from dotenv import load_dotenv

# =============================================================================
# --- FUNCIÓN AUXILIAR: TRADUCTOR DE OPERADORES ---
# =============================================================================
def aplicar_operador(col_izq, col_der, op):
    op = str(op).strip()
    if op in ['=', '==']: return col_izq == col_der
    elif op in ['<>', '!=']: return col_izq != col_der
    elif op == '>': return col_izq > col_der
    elif op == '<': return col_izq < col_der
    elif op == '>=': return col_izq >= col_der
    elif op == '<=': return col_izq <= col_der
    return col_izq == col_der  

# =============================================================================
# --- FUNCIÓN BASE: CARGA INDIVIDUAL DEL UNIVERSO ---
# =============================================================================
def cargar_contexto_universo(nombre_universo):
    # Aplicación de Políticas de Nomenclatura
    DATA_UNIVERSE_UPPER = nombre_universo.strip().upper()
    DATA_UNIVERSE_LOWER = nombre_universo.strip().lower()
    
    # Grupo 1: Archivos Físicos (MAYÚSCULAS)
    ARCHIVO_EXCEL = f'{DATA_UNIVERSE_UPPER}.xlsm'
    ambiente = f'{DATA_UNIVERSE_UPPER}.env'
    
    if not os.path.exists(ARCHIVO_EXCEL):
        print(f"     [!] ERROR CRÍTICO: No se encontró el Excel. Debe llamarse exactamente: {ARCHIVO_EXCEL}")
        return None
    if not os.path.exists(ambiente):
        print(f"     [!] ERROR CRÍTICO: No se encontró el archivo de credenciales. Debe llamarse exactamente: {ambiente}")
        return None

    # Grupo 2: Estructura Interna (MAYÚSCULAS)
    SHEET_BASE = 'BASE'
    SHEET_RELACIONES = f'RELACIONES {DATA_UNIVERSE_UPPER}'
    SHEET_VARIABLES = f'VARIABLES_{DATA_UNIVERSE_UPPER}'
    PREFIJO_VISTA = f'{DATA_UNIVERSE_UPPER}_VISTA_'

    load_dotenv(dotenv_path=ambiente, override=True)

    usuario = os.getenv('DS_USR')
    contrasena = os.getenv('DS_PSW')
    dserver = os.getenv('DS_NAME')
    dbase = os.getenv('DS_DB')

    if not all([usuario, contrasena, dserver, dbase]):
        print(f"     [!] ADVERTENCIA: Faltan credenciales en {ambiente}")
        return None

    Base = declarative_base() 

    try:
        df = pd.read_excel(ARCHIVO_EXCEL, sheet_name=SHEET_BASE)
        df_relations = pd.read_excel(ARCHIVO_EXCEL, sheet_name=SHEET_RELACIONES)
        df_vars = pd.read_excel(ARCHIVO_EXCEL, sheet_name=SHEET_VARIABLES)
    except Exception as e:
        print(f"     [!] ERROR al leer las pestañas del Excel. Se esperaban: {SHEET_BASE}, {SHEET_RELACIONES}, {SHEET_VARIABLES}. Detalles: {e}")
        return None

    orm_classes = {}
    reflected_views = {}
    
    sqlalchemy_connection_string = f"mssql+pyodbc://{usuario}:{contrasena}@{dserver}/{dbase}?driver=ODBC+Driver+17+for+SQL+Server&TrustServerCertificate=yes"
    engine = create_engine(sqlalchemy_connection_string)
    metadata = MetaData()
    pyodbc_connection_string = f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={dserver};DATABASE={dbase};UID={usuario};PWD={contrasena}'

    # 1. Creación de Vistas y Clases 
    for index, row in df.iterrows():
        class_name = str(row['TABLA']).strip()
        query_text = str(row['QUERY']).strip()
        pk_raw = str(row.get('PRIMARY_KEY', ''))
        pk_names = [col.strip() for col in pk_raw.split(',') if col.strip()]
        
        view_name = f'{PREFIJO_VISTA}{class_name}'
        
        try:
            with pyodbc.connect(pyodbc_connection_string) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"CREATE OR ALTER VIEW {view_name} AS {query_text}")
                    conn.commit()
        except Exception: pass 

        try:
            vista_reflejada = Table(view_name, metadata, autoload_with=engine)
            reflected_views[class_name] = vista_reflejada
            
            primary_keys_orm = []
            for col_name in pk_names:
                if col_name in vista_reflejada.c:
                    primary_keys_orm.append(vista_reflejada.c[col_name])
            
            if not primary_keys_orm: continue

            DynamicORMClass = type(class_name, (Base,), {
                '__table__': vista_reflejada,
                '__mapper_args__': {'primary_key': primary_keys_orm}
            })
            orm_classes[class_name] = DynamicORMClass
        except Exception: pass

    # 2. Mapeo de Relaciones 
    for index, rel in df_relations.iterrows():
        p_class = str(rel['parent_class']).strip()
        c_class = str(rel['child_class']).strip()
        
        if p_class in orm_classes and c_class in orm_classes:
            Parent, Child = orm_classes[p_class], orm_classes[c_class]
            ParentT, ChildT = reflected_views[p_class], reflected_views[c_class]
            
            p_cols = str(rel['parent_cols']).split(',')
            c_cols = str(rel['child_cols']).split(',')
            ops_raw = str(rel.get('operadores', '=='))
            ops = ops_raw.split(',')
            
            if len(ops) < len(p_cols): ops.extend(['=='] * (len(p_cols) - len(ops)))

            try:
                cond_list = []
                for p, c, op in zip(p_cols, c_cols, ops):
                    cond_list.append(aplicar_operador(ParentT.c[p.strip()], ChildT.c[c.strip()], op))
                
                cond = and_(*cond_list)
                f_keys = [ChildT.c[c.strip()] for c in c_cols]
                
                setattr(Parent, str(rel['parent_rel_name']).strip(), relationship(
                    c_class, primaryjoin=cond, foreign_keys=f_keys, back_populates=str(rel['child_rel_name']).strip()
                ))
                setattr(Child, str(rel['child_rel_name']).strip(), relationship(
                    p_class, primaryjoin=cond, foreign_keys=f_keys, back_populates=str(rel['parent_rel_name']).strip()
                ))
            except Exception: pass
                
    configure_mappers()

    # 3. Creación del Diccionario Semántico 
    def crear_diccionario_orm_real(df_vars_local, orm_registry, dict_key_exacto):
        universo = {}
        mapa_metadatos = {} 
        parser_regex = re.compile(r'([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\s*$')

        eval_context = dict(orm_registry)
        eval_context.update({
            'func': func, 'distinct': distinct, 'case': Case, 'and_': and_, 
            'or_': or_, 'literal': literal, 'Date': Date, 'not_': not_,
            'literal_column': literal_column, 'orm_classes': orm_registry,'select': select
        })
        eval_globals = {"__builtins__": {}}
        eval_globals.update(eval_context)
        # Grupo 3 (Instancia 5): Se inyecta la llave base en MINÚSCULAS
        eval_globals[dict_key_exacto] = universo

        def construir_ruta_limpia(row_data):
            path = []
            for col in ['CARPETA', 'SUBCARPETA', 'SUBSUBCARPETA']:
                val = row_data.get(col)
                if pd.isna(val): break
                path.append(str(val).strip())
            return path

        for index, row in df_vars_local.iterrows():
            if str(row.get('ACTIVO', 'Si')).strip().upper() != 'SI': continue
            path = construir_ruta_limpia(row)
            if not path: continue
            curr = universo
            for f in path: curr = curr.setdefault(f, {})

            nombre_var = str(row['nombre_var']).strip()
            declaracion = str(row.get('declaracion', 'Si')).strip()
            select_str = str(row.get('select', '')).strip().replace('"', '')
            ruta_cat = str(row.get('CATALOGO', '')).strip()

            try:
                orm_elem = None
                if declaracion == 'No':
                    orm_elem = eval(select_str, eval_globals)
                else:
                    match = parser_regex.search(select_str)
                    if match:
                        c, a = match.groups()
                        if c in orm_registry:
                            attr = getattr(orm_registry[c], a)
                            orm_elem = distinct(attr) if declaracion == 'Distinct' else attr

                if orm_elem is not None:
                    tipo_dato = str(row.get('tipo_dato', 'Dimension')).strip()
                    if tipo_dato != 'Filter': orm_elem = orm_elem.label(nombre_var)
                    
                    curr[nombre_var] = orm_elem
                    mapa_metadatos[ruta_cat] = {
                        'nombre_var': nombre_var,
                        'dependencias': str(row.get('DEPENDENCIAS_REQ', '')).strip(),
                        'tipo_dato': tipo_dato,
                        'agregacion': str(row.get('agregacion', 'None')).strip(),
                        'orm_obj': orm_elem
                    }
            except Exception: continue
        return universo, mapa_metadatos

    # Inyectamos DATA_UNIVERSE_LOWER (minúsculas) según política Grupo 3
    universo_semantico, mapa_metadatos = crear_diccionario_orm_real(df_vars, orm_classes, DATA_UNIVERSE_LOWER)

    # 4. TEST DE INTEGRIDAD
# 4. TEST DE INTEGRIDAD
    SessionTest = sessionmaker(bind=engine)
    session_test = SessionTest()
    resultados_test = []

    for ruta, meta in mapa_metadatos.items():
        estado_test, detalle_error = "OK", "Validación exitosa"
        
        # --- INICIALIZACIÓN PREVENTIVA ---
        meta['tipo_dato_ui'] = "Desconocido"
        meta['tipo_sql_nativo'] = "N/A"
        # ---------------------------------

        tablas_req = [t.strip() for t in meta['dependencias'].split(',') if t.strip() in orm_classes]
        
        if not tablas_req:
            estado_test, detalle_error = "ADVERTENCIA", "Sin dependencias"
        else:
            t_base = tablas_req[0]
            test_query = session_test.query(meta['orm_obj']).select_from(orm_classes[t_base])

            if len(tablas_req) > 1:
                tablas_en_test = {t_base}
                agregados = True
                while agregados:
                    agregados = False
                    for idx, r in df_relations.iterrows():
                        p = str(r['parent_class']).strip()
                        h = str(r['child_class']).strip()
                        if p in tablas_req and h in tablas_req:
                            p_cols = str(r['parent_cols']).split(',')
                            h_cols = str(r['child_cols']).split(',')
                            ops = str(r.get('operadores', '==')).split(',')
                            if len(ops) < len(p_cols): ops.extend(['=='] * (len(p_cols) - len(ops)))
                            
                            if p in tablas_en_test and h not in tablas_en_test:
                                cond_test = and_(*[aplicar_operador(getattr(orm_classes[p], pc.strip()), getattr(orm_classes[h], hc.strip()), o) for pc, hc, o in zip(p_cols, h_cols, ops)])
                                test_query = test_query.outerjoin(orm_classes[h], cond_test)
                                tablas_en_test.add(h); agregados = True
                            elif h in tablas_en_test and p not in tablas_en_test:
                                cond_test = and_(*[aplicar_operador(getattr(orm_classes[p], pc.strip()), getattr(orm_classes[h], hc.strip()), o) for pc, hc, o in zip(p_cols, h_cols, ops)])
                                test_query = test_query.outerjoin(orm_classes[p], cond_test)
                                tablas_en_test.add(p); agregados = True
            
# --- BLOQUE DE EJECUCIÓN Y EXTRACCIÓN DE TIPOS ---
            tipo_estandar = "Desconocido"
            tipo_crudo_str = "N/A"
            try:
                # 1. Bajar a nivel "Conexión Core" para eludir el ORM por completo
                conexion_core = session_test.connection()
                result = conexion_core.execute(test_query.limit(1).statement)
                
                # 2. Ahora sí, el resultado es nativo y el cursor está expuesto
                if hasattr(result, 'cursor') and result.cursor and result.cursor.description:
                    tipo_obj = result.cursor.description[0][1]
                    tipo_crudo_str = getattr(tipo_obj, '__name__', str(tipo_obj)).lower()
                    
                    if any(x in tipo_crudo_str for x in ['int', 'float', 'decimal', 'numeric', 'real']):
                        tipo_estandar = "Numerico"
                    elif any(x in tipo_crudo_str for x in ['date', 'time', 'datetime']):
                        tipo_estandar = "Fecha"
                    elif 'bool' in tipo_crudo_str:
                        tipo_estandar = "Booleano"
                    else:
                        tipo_estandar = "Texto"
                else:
                    # Fallback por si la base de datos devuelve una consulta vacía sin descripción
                    tipo_crudo_str = "sin_descripcion"
                    tipo_estandar = "Texto"
                
                result.close() # Liberamos la conexión
                
                meta['tipo_dato_ui'] = tipo_estandar
                meta['tipo_sql_nativo'] = tipo_crudo_str

            except Exception as e:
                session_test.rollback() # Limpiamos la tubería si hubo error
                estado_test, detalle_error = "ERROR BD", str(e.__dict__.get('orig', e)).replace('\n', ' ')
                meta['tipo_dato_ui'] = "Desconocido"
                meta['tipo_sql_nativo'] = "Error de Ejecución"

        # Guardamos en el CSV (ahora incluye Tipo_Dato)
        resultados_test.append({
            'Ruta': ruta, 
            'Variable': meta['nombre_var'], 
            'Tipo_Dato': meta['tipo_dato_ui'], 
            'Estado': estado_test, 
            'Error': detalle_error
        })

    session_test.close()
    pd.DataFrame(resultados_test).to_csv(f"auditoria_micro_queries_{DATA_UNIVERSE_LOWER}.csv", index=False, encoding='utf-8-sig')

    # =========================================================================
    # --- 5. DIBUJADO DEL DIAGRAMA ORM (GRAPHVIZ SVG CON OPERADORES) ---
    # =========================================================================
    def draw_orm_diagram(base, filename=f"diagrama_relaciones_{DATA_UNIVERSE_LOWER}"):
        try:
            dot = graphviz.Digraph(comment=f'Diagrama de Clases ORM {DATA_UNIVERSE_UPPER}', format='svg') 
            dot.attr(rankdir='LR') 
            mappers = base.registry.mappers
            
            # Dibujar Nodos (Tablas)
            for mapper in mappers:
                cls = mapper.class_
                cls_name = cls.__name__
                if cls_name not in orm_classes: continue 
                table_name = mapper.local_table.name
                label = f'<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4"><TR><TD COLSPAN="2" BGCOLOR="#ADD8E6"><B>{cls_name}</B></TD></TR>'
                label += f'<TR><TD COLSPAN="2" BGCOLOR="#D3D3D3" BORDER="0"><FONT POINT-SIZE="08">({table_name})</FONT></TD></TR>'
                for prop in mapper.column_attrs:
                    col = prop.columns[0]
                    pk_mark = ' (PK)' if col.primary_key else ''
                    label += f'<TR><TD ALIGN="LEFT">{col.name}{pk_mark}</TD><TD ALIGN="RIGHT"><FONT POINT-SIZE="08">{col.type}</FONT></TD></TR>'
                label += '</TABLE>>'
                dot.node(cls_name, label, shape='plain')
            
            # Dibujar Bordes (Relaciones con Operadores)
            for mapper in mappers:
                cls_name = mapper.class_.__name__
                if cls_name not in orm_classes: continue
                for name, relationship_obj in mapper.relationships.items():
                    if relationship_obj.direction.name == 'MANYTOONE': continue 
                    target_cls = relationship_obj.mapper.class_
                    
                    if hasattr(target_cls, '__name__') and target_cls.__name__ in orm_classes:
                        target_name = target_cls.__name__
                        label_fwd, label_back = name, relationship_obj.back_populates
                        
                        # ---> INYECCIÓN: Búsqueda del Operador en df_relations <---
                        operador_centro = "=="
                        # Buscamos de Padre a Hijo
                        rel_match = df_relations[(df_relations['parent_class'].str.strip() == cls_name) & (df_relations['child_class'].str.strip() == target_name)]
                        if not rel_match.empty:
                            operador_centro = str(rel_match.iloc[0].get('operadores', '==')).strip()
                        else:
                            # Buscamos inverso por si acaso (Hijo a Padre)
                            rel_match_inv = df_relations[(df_relations['parent_class'].str.strip() == target_name) & (df_relations['child_class'].str.strip() == cls_name)]
                            if not rel_match_inv.empty:
                                operador_centro = str(rel_match_inv.iloc[0].get('operadores', '==')).strip()

                        arrowhead, arrowtail = ('none', 'crow') if relationship_obj.direction.name == 'ONETOMANY' else ('crow', 'crow') if relationship_obj.direction.name == 'MANYTOMANY' else ('none', 'none')
                        
                        # Pasamos el parámetro 'label' para dibujar sobre la flecha
                        dot.edge(
                            cls_name, 
                            target_name, 
                            label=f' {operador_centro} ', # Operador matemático en medio
                            headlabel=f' {label_back} ', 
                            taillabel=f' {label_fwd} ', 
                            arrowhead=arrowhead, 
                            arrowtail=arrowtail, 
                            dir='both', 
                            fontname="Helvetica", 
                            fontsize="08",
                            fontcolor="blue", # Azul para que el operador resalte
                            color="firebrick"
                        )
            
            dot.render(filename, view=False, cleanup=True)
            print(f"     [OK] Diagrama de Entidad-Relación SVG exportado a: {filename}.svg")
        except Exception as e:
            print(f"     [!] Advertencia: No se pudo generar el diagrama SVG. Asegúrate de tener Graphviz instalado. Detalle: {e}")

    draw_orm_diagram(Base)

    return {
        'engine': engine,
        'orm_classes': orm_classes,
        'df_relations': df_relations,
        'universo_semantico': universo_semantico,
        'mapa_metadatos': mapa_metadatos
    }

def inicializar_motor_bi(archivo_maestro='UNIVERSE_MASTER_REGISTRY.xlsx'):
    pool = {}
    try:
        df_master = pd.read_excel(archivo_maestro, sheet_name='UNIVERSES')
        df_master.columns = df_master.columns.str.strip().str.upper() 
    except Exception as e:
        print(f"[!] ERROR CRÍTICO: No se pudo leer el archivo maestro. Detalles: {e}")
        return pool

    columnas_esperadas = ['UNIVERSE', 'ACTIVO']
    if not all(col in df_master.columns for col in columnas_esperadas):
        return pool

    for index, row in df_master.iterrows():
        if pd.isna(row.get('UNIVERSE')): continue
        nombre_universo = str(row['UNIVERSE']).strip()
        activo_str = str(row['ACTIVO']).strip().upper()

        if activo_str == 'SI':
            print(f"\n[+] Universo '{nombre_universo}' -> ACTIVO. Iniciando carga...")
            contexto = cargar_contexto_universo(nombre_universo)
            
            if contexto:
                llave_pool = nombre_universo.upper()
                pool[llave_pool] = contexto
                print(f"     [OK] Universo guardado en el Pool de Memoria bajo la llave: '{llave_pool}'")
    return pool
# Universos
## Motor de Capa Semántica Dinámica (Python + SQLAlchemy)
Este proyecto implementa un motor de BI capaz de transformar definiciones de negocio almacenadas en Excel en consultas SQL complejas de forma dinámica.

### Características Principales
Reflexión Automática de Vistas: Crea clases ORM en tiempo de ejecución basándose en vistas de SQL Server.

Inyección de Lógica Semántica: Evalúa fórmulas de negocio mediante eval() dentro de un contexto controlado de SQLAlchemy.

Gestión de Relaciones Automática: Resuelve joins complejos analizando las dependencias de las variables solicitadas.

Validación de Micro-Queries: Incluye un test de integridad que valida cada variable contra la base de datos antes de habilitarla.

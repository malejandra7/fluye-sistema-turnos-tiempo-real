# Propuesta: de dispensador de turnos a sistema que piensa

## El problema de partida

Un centro con kiosco y 4 módulos entregaba un cupo fijo de 20 turnos por media hora, es decir 40 por hora. Con 8 minutos por atención, 4 módulos atienden 30 por hora. Sobraban unas 10 personas cada hora, así que al mediodía había más de 40 de atraso. Además, como no había pausas planificadas, el equipo no alcanzaba ni a desayunar.

La causa no era la gente, sino los **números fijos**: un cupo que ignora cuántos módulos están atendiendo, un tiempo de atención que nadie mide y unas pausas que dependen de que «haya un hueco».

## Qué hacen los referentes

| Referente | Qué aporta | Qué le falta para este caso |
|---|---|---|
| **Qmatic Orchestra** ([producto](https://www.qmatic.com/products/orchestra/)) | Líder del mercado: kioscos, pantallas, voz, turno móvil y espera estimada | Es una plataforma empresarial: cara, se instala en sitio y es pesada para un centro pequeño |
| **Q-Flow / Qnomy** ([producto](https://www.qnomy.com/orchestration-platform/queue-management/)) | Orquesta todo el recorrido del cliente, balancea la carga y gestiona prioridades | Igual: enfoque corporativo |
| **qtrcipher/queue-management-system** ([GitHub](https://github.com/qtrcipher/queue-management-system)) | Código abierto moderno: NestJS, React, Prisma, WebSocket, multisede, QR, Apache 2.0 | Su propia documentación no cubre enrutamiento inteligente, prioridad, pausas ni analítica avanzada |
| **opengovsg/queuesg** ([GitHub](https://github.com/opengovsg/queuesg)) | Filas emergentes del gobierno de Singapur; recordatorio por SMS | Archivado en 2024; usa Trello como backend |
| **Itssumarfarooq/Queue-Management-System**, **bilygc/nodejs-websocket-ticket-app** | Patrón Express + Socket.IO para pantallas en vivo | Solo la parte básica |
| Soluciones por WhatsApp (Wavetec, Orion) | Fila virtual con avisos por WhatsApp | Canal, no motor |

**Conclusión:** el mercado resuelve bien el «dispensar y llamar». El hueco que Fluye ocupa está en tres lugares: **capacidad que se ajusta a las pausas**, **cuidado del equipo** y **tiempos aprendidos** para un centro mediano que no puede pagar un Qmatic.

## Respaldo de las decisiones

- **Erlang C para dotación.** Es el modelo estándar de los call centers. Tiene límites conocidos: supone paciencia infinita, y cuando hay mucho abandono Erlang A es más realista ([soon.works](https://soon.works/blog/understanding-erlang-c-erlang-a-and-simulation-for-workforce-management), [estudio en aerolínea](https://www.researchgate.net/profile/Kaushik-Nag-3/publication/323137574_Evaluating_erlang_C_and_erlang_A_models_for_staff_optimization_A_case_study_in_an_airline_call_center/links/6388552bfee13e4fe53019bc/Evaluating-erlang-C-and-erlang-A-models-for-staff-optimization-A-case-study-in-an-airline-call-center.pdf)). Por eso Fluye lo usa para *recomendar* dotación, mientras que la espera que ve el público sale de una simulación con el estado real de cada módulo. Esa simulación sí sabe quién está almorzando.
- **Datos antes que fórmulas.** Hay estudios que muestran que los modelos que aprenden del historial superan a Erlang C ([HAL, 2025](https://hal-lara.archives-ouvertes.fr/LGI-SR/hal-05025971v1)). Fluye arranca con un estimador robusto que mezcla el valor inicial y el historial, y deja la puerta abierta a un modelo de aprendizaje automático cuando haya meses de datos.
- **Micropausas.** Un metaanálisis encontró que las pausas de 10 minutos o menos reducen la fatiga y mejoran el vigor ([PMC9432722](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9432722/)). El efecto es pequeño pero significativo, y es mayor cuanto más larga la pausa. Otras guías recomiendan pausas cada ~90 minutos (ritmos ultradianos). De ahí los valores iniciales de 90 y 150 minutos, editables.

## Ya construido (v0.1)

- Tiempo real en todas las pantallas (WebSocket con reconexión automática).
- Toma manual de cualquier turno, «llamar siguiente» o asignación automática, por módulo.
- Toma atómica: un turno nunca queda en dos módulos.
- Cronómetro desde que se toma el turno; histórico completo de eventos.
- Tiempos aprendidos por servicio y prioridad; los preferenciales tardan más y el sistema lo sabe.
- Prioridad equilibrada con bono configurable.
- Pausas con un toque; la capacidad y las esperas se recalculan solas sin revelar quién salió.
- Recomendación de pausas justa y con cobertura mínima.
- Pantalla con voz, seguimiento por QR, kiosco táctil, panel con Erlang C, pronóstico y CSV.
- Habilidades por módulo: cada persona elige qué servicios atiende.
- Simulador para demostraciones.

## Ideas para las próximas versiones

Ordenadas por impacto para venderlo:

1. **Aviso por WhatsApp o SMS** («faltan 3 personas»), para que la gente salga a almorzar en vez de esperar de pie. Es la función que más mejora la percepción de la espera.
2. **Turno desde casa:** pedirlo por web o WhatsApp antes de llegar, con un QR en la entrada que confirma la llegada.
3. **Citas + fila en una sola cola:** las citas reservan capacidad futura y el motor ya sabe intercalarlas.
4. **Encuesta de un toque** al finalizar, en el celular de la persona: satisfacción por módulo y servicio.
5. **Tipificación al cerrar** (qué trámite fue realmente) para afinar los tiempos y detectar servicios mal clasificados.
6. **Plan del día automático:** a primera hora, Fluye propone el horario de desayunos y almuerzos con el pronóstico de llegadas, evitando la hora pico.
7. **Erlang A y abandono:** medir quién se va sin ser atendido y usarlo en la dotación.
8. **Multisede y SaaS:** sedes, roles, acceso con PIN por persona y Postgres con Redis para varios servidores.
9. **Modelo de aprendizaje automático** para la espera (hora, día, mezcla de servicios), cuando haya suficiente historial.
10. **Accesibilidad:** kiosco con lectura en voz alta y alto contraste; pantalla con lengua de señas en video.

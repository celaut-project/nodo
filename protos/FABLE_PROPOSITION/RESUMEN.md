# celaut.proto: resumen ejecutivo

Para Josemi. Objetivo, en tus palabras: *simplificar el contrato `celaut.proto` sin dejarlo
vulnerable a actualizaciones forzadas (hard fork) en el futuro.*

El análisis completo está en inglés en `ANALYSIS.md`. El contrato propuesto está en
`celaut.proposed.proto` (compila con `protoc`), y el diff contra la rama
`design/proto-quantities` está en `proposed.diff`. Todas las referencias `archivo:línea`
son del código de nodo en `dev` (`f3834aa`). No se ha publicado nada en GitHub.

1. **La rama mueve el contenido correcto fuera del contrato** (nombres de recursos, claves
   de benchmarks, de tarifas y de límites, el cero mágico de la hibernación). Pero según su
   propia regla de conteo (REASON 8.6) el contrato crece: de 90 a 91 mensajes, de 358 a 365
   campos, de 2156 a 2252 líneas.

2. **El mayor riesgo de actualización no está en los campos, está en las firmas.** Cada
   mensaje firmado (Peer, oferta, extracto, recibo, prueba de reputación) se firma sobre un
   digest de texto que enumera sus campos a mano (`node_identity.py:614-703`). Un campo
   nuevo no queda cubierto sin cambiar el digest, y los formatos de digest son claves de la
   capa `celaut-gateway` que deben ser iguales para que dos nodos se hablen
   (`transport_stack.py:935-961`). Hoy **cualquier campo nuevo en un mensaje firmado es un
   hard fork**.

3. **Solución: un sobre `Signed` que firma los bytes exactos.** Sustituye seis juegos de
   campos de firma y más de 40 claves de digest por dos mensajes. Un nodo antiguo verifica
   y reenvía un mensaje con campos que no conoce. También elimina los "64 caracteres
   hexadecimales" de la clave pública, que hoy bloquean las firmas poscuánticas.

4. **Un algoritmo de hash nuevo también parte la red hoy**, aunque `Hash.type` se diseñó
   abierto: la lista de tipos de hash es una clave de la capa que debe ser igual en los dos
   lados (`transport_stack.py:440`).

5. **Hoy un formal nuevo cambia `celaut.proto`.** Los comentarios contienen catálogos de
   formales (las políticas del slot con sus bytes exactos, las claves `pow.`, las claves de
   `share`). `compat.json` hace el hash del archivo con sus comentarios
   (`compat.py:57-60`), y `nodo update` se detiene ante cualquier diferencia. El modelo de
   dos niveles es correcto, pero el catálogo está escrito en el nivel equivocado.

6. **Un campo desconocido en un `Service` se ignora (fail open).** Es lo contrario de la
   regla 3.2 para un requisito. Se arregla con una frase: un nodo no ejecuta un servicio
   que tiene un número de campo que no conoce.

7. **La oferta no tiene lista de formales.** REASON 3.2 usa "lo que hace el nodo cuando
   cierra su ventana" como ejemplo de formal en una oferta, pero `EstimatedCost` guarda un
   modelo de precios en campos, con un `float` dentro de un mensaje firmado (contra la
   regla 6.1), y `OnClose` es un enum. Propuesta: `EstimatedCost.terms` y `Peer.terms`.

8. **Los dos enums incumplen la regla 4.4.** El propio comentario de `Device.Attach` ya
   prevé valores que el lector no conoce. La propuesta no tiene ningún enum.

9. **(tags, prose, formal) existe ocho veces, con cuatro reglas de comparación distintas
   en el código.** REASON 2.2 permite una sola. Propuesta: un único `Descriptor`, y un
   esquema de firma que es un solo descriptor, no un conjunto de componentes emparejados
   por permutación.

10. **La rama reutiliza números de campo con tipos nuevos.** Lo ejecuté con los dos
    esquemas reales: un nodo en `dev` lee el disco de un servicio nuevo como su límite de
    memoria, sin ningún error. REASON no tiene regla sobre números de campo, y la necesita.

11. **REASON.md es sólido, pero no está completo.** Las reglas 1.3 y 1.4 dan dos
    respuestas para un esquema de firma. El test elemental no se puede decidir tal como
    está escrito. La regla 1.6 no tiene ancla de confianza (¿quién dice que este servicio
    implementa este formal?). La regla 6.2 describe un estado que el código no tiene. La
    sección 3 de `ANALYSIS.md` da una redacción exacta para cada caso.

12. **Prueba de estrés con 15 escenarios futuros:** la rama absorbe 3 solo con formales, y
    2 en parte. La propuesta absorbe 12, y 3 en parte.

13. **El contrato propuesto:** de 91 a 65 mensajes, de 365 a 240 campos, de 2 a 0 enums,
    de 25 a 22 RPC. De esa reducción, 11 mensajes, 65 campos y 3 RPC (Chat y Observe) se
    mueven a protocolos opcionales, no se borran. Cada cambio es independiente y está
    ordenado por valor y riesgo.

14. **PENDING.md:** hay nueve recomendaciones en la sección 4. Las principales: la forma de
    continuar por defecto sigue siendo "arranque nuevo desde el entrypoint", el
    multiplicador de escasez es un término del nodo, el vínculo "este servicio implementa
    este formal" es una opinión de reputación, y un esquema de firma tiene un solo formal.

15. **Los renombrados y los cambios de tipo hay que hacerlos ahora.** En la comparación de
    capas, el nombre de un mensaje forma parte de la superficie congelada. Después de la
    congelación solo dos cambios son seguros: un formal nuevo, y un campo nuevo en un
    anuncio firmado sobre sus bytes.

## Grado de certeza

- **Comprobado en el código o ejecutado:** puntos 1, 2, 4, 5, 6, 8, 9, 10 y los conteos del
  punto 13.
- **Opinión de diseño** (otro diseñador razonable podría decidir distinto): la forma
  concreta de `Signed`, mover el modelo de precios y la ventana de actividad a `terms`, y
  sacar Chat y Observe del contrato.
- **No verificado:** no ejecuté nodo ni sus tests, no leí la TUI en Rust ni los PR en
  borrador de dispositivos (#649), que la propuesta toca.

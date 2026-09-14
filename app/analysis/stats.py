"""Correlaciones sin numpy, y sobre todo sin ceros de relleno.

POR QUÉ ESTO NO DEVUELVE NÚMEROS A SECAS
----------------------------------------
Una correlación es un número entre -1 y 1 y no dice nada por sí sola. `r=0.62`
sobre ocho días y `r=0.62` sobre ciento cincuenta son afirmaciones distintas, y
la primera es casi siempre ruido. Peor todavía: cuando no se puede calcular -dos
columnas de la misma constante, tres pares- la tentación del código es devolver
`0.0` o `None`, y las dos mienten. `0.0` dice "no hay relación", que es una
conclusión; `None` no dice nada y se pinta como un hueco indistinguible de "esa
pareja no se ha mirado".

Por eso todo lo de aquí devuelve un `Resultado` que lleva SIEMPRE:

    n         cuántos pares entraron de verdad
    ventana   entre qué fechas
    r         el número, o None
    na        si r es None, POR QUÉ, en castellano y en concreto
    suficiente  si n llega al mínimo para creérselo

`na` y `r` son excluyentes: o hay número o hay motivo. Nunca los dos vacíos.

POR QUÉ HAY p, SI NADIE LO PIDIÓ
--------------------------------
Porque la vista 3 pregunta por el ranking de ejercicios por correlación con la
molestia lumbar del día siguiente, y un ranking es una trampa: con quince
ejercicios y ruido puro, el primero de la lista sale con `r` alto casi seguro.
Sin `p` no hay forma de distinguir "este ejercicio me carga la espalda" de "este
ejercicio salió primero en la lotería de esta semana", y esa distinción es
justo la que decide si se cambia un entrenamiento.

Se calcula con la t de Student y una beta incompleta regularizada propia. No hay
scipy y no lo va a haber: el contenedor es pequeño a propósito.

SPEARMAN Y NO SOLO PEARSON
--------------------------
Los deslizadores del formulario son ordinales de 1 a 5: la distancia entre un 4
y un 5 de cansancio no es la misma que entre un 1 y un 2, y Pearson da por hecho
que sí. Spearman trabaja sobre los rangos, que es lo único que un 1-5 sostiene.
Se ofrecen los dos porque contra una métrica continua de Garmin -HRV, minutos de
sueño- Pearson sí tiene sentido, y porque cuando los dos discrepan mucho eso ya
es información: significa que la relación existe pero no es una recta.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from app.analysis.texto import cuantos, plural

# Por debajo de esto no hay nada que calcular: con dos puntos la recta pasa
# exactamente por los dos y `r` sale 1 o -1 sin excepción.
N_MINIMO_CALCULABLE = 3

# El umbral de "muestra insuficiente" del usuario. NO oculta la correlación: la
# marca. Ocultarla sería decidir por él que no quiere verla, y lo que pidió
# explícitamente es lo contrario -verla, con la advertencia al lado-.
N_MINIMO_FIABLE = 20

# Decimales con los que viaja una p. Cinco son de sobra para leerla: por debajo
# de 0,00001 ya no hay decisión que dependa del dígito siguiente.
DECIMALES_P = 5
P_MINIMA = 10.0**-DECIMALES_P


def redondear_p(p: float | None) -> float | None:
    """Redondea una p SIN dejar que llegue nunca a cero exacto.

    `round(p, 5)` sobre un 3e-12 -que es lo que sale de un Spearman de 173 días
    con r = 0,4- da `0.0`, y eso viaja al navegador como «p = 0», que se lee
    como «esto es imposible por azar». Ningún contraste dice eso jamás: dice
    «más improbable de lo que sé medir con la precisión con la que lo cuento».

    Es el mismo fallo de siempre con otro disfraz: el valor que se lee no es el
    valor que se calculó, y la diferencia entre los dos la introduce la
    presentación sin avisar. Aquí el suelo es `P_MINIMA`, y quien lo vea sabe
    que significa «esto o menos», no «esto exactamente». Cero se reserva para lo
    que de verdad es cero, que no existe.
    """
    if p is None:
        return None
    if p <= 0.0:
        # Una p negativa o nula no la produce ningún contraste de aquí; si
        # aparece es un error de cálculo aguas arriba y no se disimula.
        return 0.0 if p == 0.0 else p
    return max(round(p, DECIMALES_P), P_MINIMA)


@dataclass(frozen=True)
class Resultado:
    """Una correlación, con todo lo que hace falta para no malinterpretarla."""

    n: int
    metodo: str  # pearson | spearman
    r: float | None = None
    na: str | None = None
    p: float | None = None
    desde: date | None = None
    hasta: date | None = None
    # Cuántos pares se descartaron por faltar uno de los dos lados. Se cuenta
    # porque un n bajo tiene dos causas muy distintas -"llevo poco tiempo" y
    # "falta la mitad de los datos"- y la segunda se arregla.
    descartados: int = 0

    @property
    def suficiente(self) -> bool:
        return self.r is not None and self.n >= N_MINIMO_FIABLE

    @property
    def aviso(self) -> str | None:
        """Lo que hay que poner al lado del número para que no engañe."""
        if self.r is None:
            return self.na
        if self.n < N_MINIMO_FIABLE:
            return (
                f"muestra insuficiente: {self.n} pares, hacen falta "
                f"{N_MINIMO_FIABLE} para tomárselo en serio"
            )
        return None

    def como_dict(self) -> dict:
        """Lo que viaja al navegador. La PWA pinta esto y no calcula nada."""
        return {
            "n": self.n,
            "metodo": self.metodo,
            "r": None if self.r is None else round(self.r, 4),
            "p": redondear_p(self.p),
            "na": self.na,
            "aviso": self.aviso,
            "suficiente": self.suficiente,
            "descartados": self.descartados,
            "desde": self.desde.isoformat() if self.desde else None,
            "hasta": self.hasta.isoformat() if self.hasta else None,
            # Las dos claves de la corrección por comparaciones múltiples viajan
            # SIEMPRE, y a `None` cuando no se ha corregido. `None` aquí no es
            # "no se pudo": es "sobre esta vista no se pasó ninguna corrección",
            # que es un hecho de la vista y no del número.
            #
            # Van aunque no se usen porque la alternativa es peor. La PWA dibuja
            # la barra hueca con `c.significativa !== false`, y sin la clave eso
            # es leer algo que no viene: `undefined !== false` da `true` y la
            # barra sale sólida. Funciona, pero por accidente -nadie lo decidió-
            # y el día que alguien invierta la comprobación, la vista entera
            # cambia de significado sin un solo error. Quien lea esto tiene que
            # poder distinguir "no aguanta la corrección" de "aquí no hay
            # corrección que aguantar".
            #
            # `corregir_tanda` las sobrescribe con los valores de verdad.
            "p_corregida": None,
            "significativa": None,
        }


# ---------------------------------------------------------------------------
# Emparejar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pares:
    """Dos series alineadas por fecha, ya sin huecos.

    Emparejar es la mitad del trabajo y la mitad que se hace mal. Dos diccionarios
    fecha -> valor no se pueden recorrer en paralelo: cada uno tiene sus propios
    días ausentes, y un `zip` sobre las listas de valores emparejaría el
    cansancio del martes con el HRV del jueves sin que nada chirriara. El
    resultado sería una correlación perfectamente calculada de dos cosas que no
    se corresponden.
    """

    dias: list[date] = field(default_factory=list)
    x: list[float] = field(default_factory=list)
    y: list[float] = field(default_factory=list)
    descartados: int = 0

    def __len__(self) -> int:
        return len(self.dias)


def emparejar(
    xs: dict[date, float | None], ys: dict[date, float | None], *, desfase: int = 0
) -> Pares:
    """Empareja por fecha, descartando el día en que falte cualquiera de los dos.

    `desfase` desplaza la SEGUNDA serie: el día `d` de `xs` se empareja con el
    día `d + desfase` de `ys`. Ver `mejor_desfase` para qué significa el signo.

    Un `None` en cualquiera de los dos lados tira el par entero. No se rellena
    con la media ni con el último valor: eso inventa días, y días inventados en
    una correlación son exactamente la forma de encontrar relaciones que no
    existen.
    """
    from datetime import timedelta

    dias, vx, vy, fuera = [], [], [], 0
    for d in sorted(xs):
        a = xs.get(d)
        b = ys.get(d + timedelta(days=desfase))
        if a is None or b is None:
            fuera += 1
            continue
        dias.append(d)
        vx.append(float(a))
        vy.append(float(b))
    return Pares(dias=dias, x=vx, y=vy, descartados=fuera)


# ---------------------------------------------------------------------------
# Los coeficientes
# ---------------------------------------------------------------------------


def _varianza_nula(vals: Sequence[float]) -> bool:
    return max(vals) == min(vals)


def _pearson_crudo(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    r = sxy / math.sqrt(sxx * syy)
    # El redondeo de coma flotante puede sacar 1.0000000000000002, y eso revienta
    # la t de Student al dividir por cero. Se sujeta al rango que la definición
    # garantiza.
    return max(-1.0, min(1.0, r))


def rangos(vals: Sequence[float]) -> list[float]:
    """Rangos 1..n con media en los empates.

    Los empates importan aquí más que en casi ningún sitio: los deslizadores son
    enteros de 1 a 5 sobre ciento ochenta días, así que hay empates por docenas.
    Asignarlos por orden de llegada -que es lo que sale de un `sorted` ingenuo-
    inventa un orden entre días idénticos y mete ruido con estructura, que es el
    peor tipo de ruido porque no se promedia hasta cero.
    """
    n = len(vals)
    orden = sorted(range(n), key=lambda i: vals[i])
    salida = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[orden[j + 1]] == vals[orden[i]]:
            j += 1
        medio = (i + j) / 2 + 1  # rangos desde 1
        for k in range(i, j + 1):
            salida[orden[k]] = medio
        i = j + 1
    return salida


def _p_valor(r: float, n: int) -> float | None:
    """Probabilidad a dos colas de ver un |r| así de grande sin relación alguna."""
    if n <= 2 or abs(r) >= 1.0:
        return 0.0 if abs(r) >= 1.0 and n > 2 else None
    gl = n - 2
    t = abs(r) * math.sqrt(gl / (1 - r * r))
    return _beta_incompleta(gl / (gl + t * t), gl / 2, 0.5)


def _beta_incompleta(x: float, a: float, b: float) -> float:
    """Beta incompleta regularizada I_x(a, b), por fracción continua.

    Es el mínimo imprescindible para tener una t de Student sin scipy. La
    fracción continua de Lentz converge rápido en el rango que se usa aquí
    (grados de libertad de 1 a unos 200) y está acotada a 200 iteraciones para
    que no pueda colgar un endpoint pase lo que pase.
    """
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    # La fracción continua solo converge bien de un lado; del otro se usa la
    # simetría I_x(a,b) = 1 - I_{1-x}(b,a).
    if x > (a + 1) / (a + b + 2):
        return 1.0 - _beta_incompleta(1 - x, b, a)

    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    delante = math.exp(a * math.log(x) + b * math.log(1 - x) - lbeta) / a

    TINY = 1e-30
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    if abs(d) < TINY:
        d = TINY
    d = 1 / d
    h = d
    for m in range(1, 201):
        m2 = 2 * m
        # Paso par
        num = m * (b - m) * x / ((a + m2 - 1) * (a + m2))
        d = 1 + num * d
        if abs(d) < TINY:
            d = TINY
        c = 1 + num / c
        if abs(c) < TINY:
            c = TINY
        d = 1 / d
        h *= d * c
        # Paso impar
        num = -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1))
        d = 1 + num * d
        if abs(d) < TINY:
            d = TINY
        c = 1 + num / c
        if abs(c) < TINY:
            c = TINY
        d = 1 / d
        salto = d * c
        h *= salto
        if abs(salto - 1) < 1e-12:
            break
    return max(0.0, min(1.0, delante * h))


def correlacion(pares: Pares, *, metodo: str = "spearman") -> Resultado:
    """El coeficiente, o el motivo por el que no lo hay. Nunca un cero de relleno."""
    desde = pares.dias[0] if pares.dias else None
    hasta = pares.dias[-1] if pares.dias else None
    base = dict(
        n=len(pares), metodo=metodo, desde=desde, hasta=hasta,
        descartados=pares.descartados,
    )

    if len(pares) < N_MINIMO_CALCULABLE:
        falta = N_MINIMO_CALCULABLE - len(pares)
        return Resultado(
            na=(
                f"solo hay {cuantos(len(pares), 'día', 'días')} con las dos "
                f"cosas medidas; {plural(falta, 'falta', 'faltan')} {falta} "
                "para poder calcular nada"
                + (f" (y se {plural(pares.descartados, 'descartó', 'descartaron')}"
                   f" {pares.descartados} por huecos)"
                   if pares.descartados else "")
            ),
            **base,
        )

    if metodo not in ("spearman", "pearson"):
        raise ValueError(f"método desconocido: {metodo!r}")

    # El motivo se compone con los valores ORIGINALES, no con los rangos, y esto
    # no es cosmética. Una serie constante a 3 tiene todos los rangos a 15.5 con
    # treinta días: el mensaje diría "vale siempre 15.5", que no es ningún valor
    # que el usuario haya visto nunca ni que esté en la base de datos. Un motivo
    # que manda a buscar un número inexistente es peor que no dar motivo.
    #
    # Y se mira ANTES de dividir, para poder decir CUÁL de los dos lados es el
    # plano: "no varía" a secas obliga a ir a mirar la base de datos igual.
    plana_x, plana_y = _varianza_nula(pares.x), _varianza_nula(pares.y)
    if plana_x and plana_y:
        return Resultado(na="ninguna de las dos series varía: las dos son constantes", **base)
    if plana_x:
        return Resultado(
            na=f"la primera serie vale siempre {pares.x[0]:g}: sin variación "
               f"no hay relación que medir",
            **base,
        )
    if plana_y:
        return Resultado(
            na=f"la segunda serie vale siempre {pares.y[0]:g}: sin variación "
               f"no hay relación que medir",
            **base,
        )

    x, y = pares.x, pares.y
    if metodo == "spearman":
        x, y = rangos(x), rangos(y)

    r = _pearson_crudo(x, y)
    if r is None:  # pragma: no cover - lo de arriba ya lo ha cazado
        return Resultado(na="varianza cero al calcular", **base)
    return Resultado(r=r, p=_p_valor(r, len(pares)), **base)


# ---------------------------------------------------------------------------
# El desfase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Desfase:
    """El barrido de retardos completo, y cuál gana.

    `por_desfase` va entero a propósito: la pregunta de la vista 2 no es solo
    "¿cuál es el máximo?" sino "¿cómo se comporta alrededor?". Un pico aislado
    en -2 con los vecinos en cero es ruido; una curva que sube hasta -2 y baja
    es una relación con retardo de verdad.
    """

    por_desfase: dict[int, Resultado] = field(default_factory=dict)
    mejor: int | None = None
    na: str | None = None

    @property
    def resultado(self) -> Resultado | None:
        return None if self.mejor is None else self.por_desfase[self.mejor]

    def como_dict(self) -> dict:
        return {
            "mejor_desfase": self.mejor,
            "na": self.na,
            "lectura": self.lectura(),
            "por_desfase": {
                str(k): v.como_dict() for k, v in sorted(self.por_desfase.items())
            },
        }

    def lectura(self) -> str | None:
        """El desfase en castellano, que es lo que se pidió saber.

        La vista 2 existe para contestar si la percepción se adelanta al cuerpo o
        va por detrás. Un `-2` en una tabla no contesta eso sin que alguien
        recuerde el convenio de signos, así que el convenio se escribe aquí y se
        manda ya traducido.
        """
        if self.mejor is None:
            return None
        r = self.por_desfase[self.mejor]
        if r.r is None:
            return None
        d = self.mejor
        if d == 0:
            return "van a la vez: lo que notas ese día es lo que marca el reloj ese día"
        if d > 0:
            return (
                f"tu percepción se ADELANTA {cuantos(d, 'día', 'días')}: lo que "
                f"notas hoy se parece a lo que el reloj marcará dentro de "
                f"{cuantos(d, 'día', 'días')}"
            )
        return (
            f"tu percepción va por DETRÁS {cuantos(abs(d), 'día', 'días')}: lo que "
            f"notas hoy se parece a lo que el reloj marcó hace "
            f"{cuantos(abs(d), 'día', 'días')}"
        )


def mejor_desfase(
    xs: dict[date, float | None],
    ys: dict[date, float | None],
    *,
    rango: range = range(-3, 4),
    metodo: str = "spearman",
) -> Desfase:
    """Correlaciona con retardos y devuelve el barrido entero más el ganador.

    EL CONVENIO DE SIGNOS, QUE ES LO ÚNICO QUE HAY QUE ENTENDER AQUÍ
    ---------------------------------------------------------------
    Un desfase `k` empareja `xs[d]` con `ys[d + k]`.

        k > 0  la métrica del reloj llega DESPUÉS de lo que él anotó. Su
               percepción se adelanta: hoy se siente hecho polvo y el HRV se
               hunde pasado mañana.
        k < 0  la métrica del reloj ya había pasado. Su percepción va por
               detrás: el cuerpo se resintió el lunes y él lo nota el miércoles.

    Gana el `|r|` más grande, no el `r` más grande: una correlación de -0.7 es
    una relación más fuerte que una de 0.3, y quedarse con la positiva por ser
    positiva es confundir la dirección con la fuerza. La dirección la lleva el
    signo del `r` que se devuelve.

    Los empates se rompen por el desfase MÁS PEQUEÑO en valor absoluto, y 0 antes
    que nada. Sin criterio explícito el ganador dependería del orden de
    iteración; y ante dos explicaciones igual de buenas, la de menos retardo es
    la que menos maquinaria necesita.
    """
    por: dict[int, Resultado] = {}
    for k in rango:
        por[k] = correlacion(emparejar(xs, ys, desfase=k), metodo=metodo)

    calculables = [k for k, r in por.items() if r.r is not None]
    if not calculables:
        # El motivo se toma del desfase 0 si está, y si no del primero: son
        # todos la misma pega -no hay días, o no varía- y repetirla siete veces
        # no aclara nada.
        ref = por.get(0) or por[next(iter(por))]
        return Desfase(por_desfase=por, na=ref.na)

    mejor = min(calculables, key=lambda k: (-abs(por[k].r or 0.0), abs(k), k))
    return Desfase(por_desfase=por, mejor=mejor)


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def percentil_de(valor: float, muestra: Sequence[float]) -> float | None:
    """En qué percentil cae `valor` dentro de `muestra`, de 0 a 100.

    Se usa el percentil MEDIO (los que están por debajo, más la mitad de los
    empatados). Con los deslizadores enteros de 1 a 5 el empate es la norma, y
    contar "estrictamente menores" mandaría todos los 1 al percentil 0 y todos
    los 5 al 80: la escala entera se desplazaría hacia abajo y un día normal
    parecería malo.

    Devuelve None con la muestra vacía. Esto alimenta la vista 5, que compara
    percepción contra rendimiento, y ahí un 0 inventado sería exactamente la
    conclusión que la vista existe para no dar por descontado.
    """
    if not muestra:
        return None
    menores = sum(1 for v in muestra if v < valor)
    iguales = sum(1 for v in muestra if v == valor)
    return 100.0 * (menores + iguales / 2) / len(muestra)


def percentil(muestra: Sequence[float], q: float) -> float | None:
    """El valor que deja por debajo el `q`% de la muestra, interpolando.

    Es el método lineal de siempre (el `numpy.percentile` por defecto), escrito
    a mano porque aquí no hay numpy.
    """
    if not muestra:
        return None
    orden = sorted(muestra)
    if len(orden) == 1:
        return float(orden[0])
    pos = (len(orden) - 1) * max(0.0, min(100.0, q)) / 100
    bajo = math.floor(pos)
    alto = math.ceil(pos)
    if bajo == alto:
        return float(orden[bajo])
    return float(orden[bajo] + (orden[alto] - orden[bajo]) * (pos - bajo))


# ---------------------------------------------------------------------------
# Comparaciones múltiples
# ---------------------------------------------------------------------------


def benjamini_hochberg(ps: Sequence[float | None]) -> list[float | None]:
    """Corrige una tanda de p-valores por el número de veces que se ha mirado.

    POR QUÉ HACE FALTA
    ------------------
    El ranking de ejercicios de la vista 3 compara treinta ejercicios contra la
    molestia lumbar del día siguiente. Con treinta intentos y un umbral del 5%,
    lo esperable es que un ejercicio y medio salga "significativo" AUNQUE NINGUNO
    tenga nada que ver con la lumbar. Y no saldría en el puesto quince: saldría
    el primero, porque el ranking está ordenado precisamente por eso.

    Es la forma más fácil de que este sistema haga daño. No calcula mal: calcula
    bien treinta veces y la presentación hace el resto. Un ranking sin corregir
    diría "el peso muerto te destroza la lumbar" con la misma cara con la que
    diría algo cierto, y la consecuencia sería dejar de hacer un ejercicio por
    ruido.

    POR QUÉ BENJAMINI-HOCHBERG Y NO BONFERRONI
    ------------------------------------------
    Bonferroni divide por treinta y se lleva por delante también lo que sí
    existe. Aquí interesa lo contrario: es un panel para mirar, no un ensayo
    clínico, y perder un efecto real es peor que tolerar que uno de cada veinte
    de los que sobreviven sea casualidad. Benjamini-Hochberg controla justo esa
    proporción -la de falsos entre los que se declaran-, que es la pregunta que
    de verdad se hace al mirar un ranking.

    Los `None` -las casillas que no se pudieron calcular- se quedan como `None` y
    NO cuentan para el tamaño de la tanda. Contarlas haría la corrección más
    dura cuantos menos datos hubiera, que es exactamente al revés de como tiene
    que comportarse.
    """
    indices = [i for i, p in enumerate(ps) if p is not None]
    m = len(indices)
    if m == 0:
        return [None] * len(ps)

    # Orden creciente de p, y el ajuste se propaga hacia atrás para que la
    # secuencia corregida no pueda bajar: si el p-valor 7 corregido sale menor
    # que el 6, el 6 se queda con el del 7. Sin ese paso la lista corregida
    # podría desordenar el ranking respecto a la original, que sería peor que no
    # corregir.
    orden = sorted(indices, key=lambda i: ps[i])  # type: ignore[index]
    corregidos: list[float | None] = [None] * len(ps)
    minimo = 1.0
    for rango in range(m, 0, -1):
        i = orden[rango - 1]
        valor = ps[i] * m / rango  # type: ignore[operator]
        minimo = min(minimo, valor)
        corregidos[i] = min(1.0, minimo)
    return corregidos


def corregir_tanda(grupos: Sequence[Sequence[dict]]) -> None:
    """Añade la p corregida a cada casilla, corrigiendo sobre la tanda ENTERA.

    Sobre la tanda entera y no grupo por grupo: lo que hay que corregir es el
    número de veces que se ha mirado, y se ha mirado una vez por casilla. Hacerlo
    por grupos daría una corrección más suave y perfectamente inútil, que es peor
    que no hacerla, porque además tranquiliza.

    Los grupos existen solo porque quien llama tiene sus casillas repartidas en
    filas -una por exposición, una por pareja- y aplanarlas en el sitio de la
    llamada sería una comprensión de lista idéntica en cada uno.

    Modifica las casillas donde están. Reciben dos claves más -`p_corregida` y
    `significativa`- y nadie tiene que acordarse de volver a colocarlas.

    Vive aquí y no en `impacto`, que es donde nació, porque desde que la vista 1
    corrige sus correlaciones internas de Garmin hay dos módulos que la usan, y
    la alternativa era que `concordancia` importara un guión bajo de `impacto`.
    Dos vistas que se corrigen con dos copias de esta función es la forma
    tranquila de que una de las dos deje de corregirse el día que se toque la
    otra, y eso no daría ningún error: daría más casillas en negrita.
    """
    plano = [c for grupo in grupos for c in grupo]
    for c, pc in zip(plano, benjamini_hochberg([c.get("p") for c in plano])):
        c["p_corregida"] = redondear_p(pc)
        c["significativa"] = bool(pc is not None and pc < 0.05)

"""Que un doble no pueda separarse de su original sin que salte un test.

POR QUÉ EXISTE ESTO
-------------------
En tres días aparecieron cinco defectos con exactamente la misma forma: una
clase de mentira escrita a mano en los tests que tenía algo que el original no
tiene, o le faltaba algo que el original sí tiene. Los cinco estaban en verde, y
los cinco certificaban una conducta que en producción es inalcanzable:

  - `SendResult` no tiene campo `ok`, pero el doble de Telegram sí. El código
    leía `getattr(envio, "ok", True)`, o sea que contra el cliente real la rama
    del fallo NO SE PODÍA EJECUTAR. El test que se llamaba «un cliente que dice
    que no envió no se pinta verde» llevaba meses en verde probando nada.
  - `HevyClient` no tiene `update_routine` ni `create_routine`, pero el doble
    del diagnóstico ponía las dos como alarma de «si escribes, te pillo». La
    alarma vigilaba una puerta que no existe.
  - el doble de los entrenamientos no mandaba `routine_id`, así que seis
    semanas de simulación eran el día 1 repetido cuarenta y dos veces.
  - `fetch_errors` era un `list` de CLASE en un doble cuyo original lo declara
    con `field(default_factory=list)`: una lista compartida entre instancias
    donde el original da una por instancia.
  - y varias firmas que aceptaban menos argumentos de los que el original
    acepta.

EL DIAGNÓSTICO NO ES «SE NOS PASÓ CINCO VECES»
----------------------------------------------
Es que nada lo miraba. Un doble se escribe una vez, el original evoluciona
después, y no hay ningún sitio donde esas dos cosas se vuelvan a encontrar. La
respuesta no puede ser acordarse: acordarse ya falló cinco veces. Tiene que ser
una comprobación que corre sola en cada batería.

CÓMO FUNCIONA
-------------
Dos capas, y la primera es la que hace que la segunda no se pueda esquivar.

1. TODA clase declarada en `tests/` tiene que decir qué es. O es doble de algo
   -`@doble_de(HevyClient)`- o declara por qué no lo es -`@no_es_doble("...")`-.
   Lo comprueba `test_dobles.py` leyendo el árbol sintáctico, así que también ve
   las clases escondidas dentro de una función o de un fixture, que son
   precisamente las que más se escapan. Una clase nueva sin declarar es un test
   rojo, no un olvido.

2. Todo lo declarado como doble se compara con su original por introspección:
   métodos y atributos que el doble tiene y el original no, firmas que aceptan
   menos de lo que el original acepta, y campos de más cuando el original es un
   dataclass. Siempre en esa dirección -lo que SOBRA-, porque lo que falta da
   un `AttributeError` ruidoso y lo que sobra no lo ve nadie.

Y una tercera comprobación que no es de dobles sino de todas: un `list`, `dict`
o `set` como atributo de CLASE es estado compartido entre instancias, que es un
defecto por sí mismo.

LO QUE ESTO NO ATRAPA, DICHO A PROPÓSITO
----------------------------------------
La introspección compara FORMA, no CONDUCTA. Un doble con los métodos correctos
que devuelve algo que el original nunca devolvería sigue siendo un doble que
miente, y eso sigue necesitando pensar. Lo que se cierra aquí es la deriva: que
el original cambie y el doble se quede como estaba.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

# Clave donde se guarda la declaración. Con nombre feo a propósito: si aparece
# escrita a mano en algún sitio que no sea este módulo, se ve.
MARCA = "__doble_declarado__"


class Declaracion:
    """Qué dijo una clase de test que es."""

    def __init__(
        self,
        original: Any | None,
        motivo: str | None = None,
        salvo: tuple[str, ...] = (),
    ) -> None:
        self.original = original
        self.motivo = motivo
        self.salvo = tuple(salvo)


def _revienta_si_diverge(cls) -> None:
    """Compara YA, en el momento de declarar, y revienta si algo no cuadra.

    Comparar aquí y no solo en el barrido de `test_dobles.py` es lo que mete en
    la red a los dobles ESCONDIDOS DENTRO DE UNA FUNCIÓN O DE UN FIXTURE, que no
    existen hasta que esa función corre y que por eso no aparecen en `vars(mod)`.
    Y son justo los que más se escapan: el `fetch_errors` de clase que había en
    `test_cli.py` estaba dentro de un test, no a nivel de módulo.

    El error sale como `AssertionError` en el momento en que el test crea la
    clase, o durante la recolección si la clase es de módulo. En los dos casos
    es rojo con el nombre del fichero y la línea puestos.
    """
    dec = declaracion_de(cls)
    problemas = list(estado_mutable_de_clase(cls))
    if dec is not None and dec.original is not None:
        problemas += divergencias(cls, dec.original)
    if problemas:
        raise AssertionError(
            f"el doble `{cls.__name__}` de {cls.__module__} no se parece a lo "
            f"que dice que es:\n  " + "\n  ".join(problemas)
        )


def doble_de(original: Any, *, salvo: tuple[str, ...] = ()):
    """Declara que esta clase hace de `original` en los tests, y lo comprueba ya.

    `salvo` son nombres que se permite que diverjan, y CADA UNO necesita su
    razón escrita al lado en el código. Es la válvula de escape, y está para
    usarse poco: si una clase necesita tres excepciones, lo que pasa es que no
    es un doble de eso. Un `salvo` que ya no excusa nada también es un fallo:
    ver el punto 4 de `divergencias`.
    """

    def envuelve(cls):
        setattr(cls, MARCA, Declaracion(original, salvo=salvo))
        _revienta_si_diverge(cls)
        return cls

    return envuelve


def no_es_doble(motivo: str):
    """Declara que esta clase no representa a nada de `app/`.

    Ayudantes de test, contenedores de datos, excepciones propias, agrupadores
    de pytest. El motivo se escribe para que la próxima persona no tenga que
    deducir si se olvidó o se decidió.

    Aunque no haya original contra el que comparar, se le pasa igualmente la
    comprobación de estado mutable de clase: un `list` compartido entre
    instancias es un defecto por sí mismo, sea la clase doble de algo o no.
    """
    if not motivo or not motivo.strip():
        raise ValueError("`no_es_doble` necesita un motivo escrito")

    def envuelve(cls):
        setattr(cls, MARCA, Declaracion(None, motivo=motivo))
        _revienta_si_diverge(cls)
        return cls

    return envuelve


def declaracion_de(cls) -> Declaracion | None:
    """La declaración PROPIA de esta clase, sin heredarla de una madre.

    `getattr` a secas encontraría la marca de la clase base y daría por
    declarada a una hija que no ha dicho nada.
    """
    return cls.__dict__.get(MARCA)


# ---------------------------------------------------------------------------
# La comparación
# ---------------------------------------------------------------------------


def _publicos(cls) -> dict[str, Any]:
    """Atributos propios y públicos, sin los heredados de `object`."""
    return {
        n: v
        for n, v in vars(cls).items()
        if not n.startswith("_") and n != MARCA
    }


def _es_metodo(v: Any) -> bool:
    return inspect.isfunction(v) or isinstance(v, (staticmethod, classmethod, property))


def _campos_declarados(cls) -> set[str]:
    """Los nombres que el original declara como datos, aunque no sean atributos.

    Hay dos formas en esta casa de decir «esta clase tiene este campo» sin que
    `hasattr` lo vea:

      - un `dataclass`, donde los campos SIN valor por defecto no dejan
        atributo de clase;
      - un modelo de pydantic -`Settings`-, que se lleva los campos a
        `model_fields` y los BORRA de la clase, así que `hasattr(Settings,
        "garmin_email")` es `False` aunque el campo exista y se use.

    Sin esto, el doble de `Settings` salía acusado de inventarse los tres
    campos que copia literalmente del original, que es exactamente al revés de
    lo que este módulo quiere decir.
    """
    fuera: set[str] = set()
    if dataclasses.is_dataclass(cls):
        fuera |= {f.name for f in dataclasses.fields(cls)}
    fuera |= set(getattr(cls, "model_fields", None) or ())
    return fuera


def _firma(cls, nombre: str) -> inspect.Signature | None:
    try:
        return inspect.signature(getattr(cls, nombre))
    except (TypeError, ValueError):
        return None


class _Params:
    """Una firma descompuesta en las tres cosas que pueden romper una llamada.

    Se separan porque las tres fallan de manera distinta y confundirlas da
    ruido. Un parámetro posicional renombrado no rompe `f(1, 2)` pero sí rompe
    `f(a=1, b=2)`; un `*args` hace que la cuenta de posicionales deje de
    importar; un argumento de solo-nombre que falte rompe siempre.
    """

    __slots__ = ("posicionales", "por_nombre", "solo_nombre", "libre_pos", "libre_kw")

    def __init__(self, sig: inspect.Signature) -> None:
        self.posicionales = 0          # cuántos aceptaría `f(1, 2, 3)`
        self.por_nombre: set[str] = set()   # los que aceptan `f(x=...)`
        self.solo_nombre: set[str] = set()  # los que SOLO aceptan `f(x=...)`
        self.libre_pos = False         # tiene `*args`
        self.libre_kw = False          # tiene `**kwargs`
        P = inspect.Parameter
        for n, p in sig.parameters.items():
            if n in {"self", "cls"}:
                continue
            if p.kind is P.VAR_POSITIONAL:
                self.libre_pos = True
            elif p.kind is P.VAR_KEYWORD:
                self.libre_kw = True
            elif p.kind is P.POSITIONAL_ONLY:
                self.posicionales += 1
            elif p.kind is P.POSITIONAL_OR_KEYWORD:
                self.posicionales += 1
                self.por_nombre.add(n)
            elif p.kind is P.KEYWORD_ONLY:
                self.por_nombre.add(n)
                self.solo_nombre.add(n)


def divergencias(doble, original) -> list[str]:
    """En qué se ha separado `doble` de `original`. Vacío = no se ha separado.

    Se mira lo que el doble AFIRMA tener, no lo que le falta: un doble parcial
    -tres métodos de los nueve- es normal y correcto. Lo que no puede es tener
    algo que el original no tiene, porque eso es exactamente lo que permite que
    el código de producción lea un campo inexistente y el test lo bendiga.
    """
    dec = declaracion_de(doble)
    salvo = set(dec.salvo) if dec else set()
    usadas: set[str] = set()   # qué excepciones han hecho falta de verdad
    fuera: list[str] = []

    def anota(nombre: str, mensaje: str) -> None:
        """Apunta una divergencia, o la excusa Y APUNTA QUE LA EXENCIÓN SIRVIÓ.

        Que `usadas` se marque AQUÍ -en el momento de tapar algo concreto- y no
        al recorrer los nombres es lo que hace que la regla 4 valga para algo.

        Antes se marcaba al principio de cada bucle, así que bastaba con que el
        nombre existiera en el doble para que la exención contara como usada.
        Consecuencia: una exención sobre algo QUE YA NO DIVERGE no se denunciaba
        nunca, y ese nombre se quedaba fuera de la comparación para siempre sin
        que nadie lo hubiera decidido. Justo el defecto que la regla 4 dice
        cerrar. Solo saltaba en el caso fácil -un nombre que el doble ni tiene-.

        Lo encontró la mutación A6 de `out/mutar_dobles.py`, que mete un
        `salvo=("login",)` sobre un método que el doble sí tiene y que no
        diverge en nada. Antes de esto: verde.
        """
        if nombre in salvo:
            usadas.add(nombre)
            return
        fuera.append(mensaje)

    dbl = _publicos(doble)
    declarados = _campos_declarados(original)

    # --- 1. lo que el doble tiene y el original no ---------------------------
    for nombre, valor in sorted(dbl.items()):
        if nombre in declarados or hasattr(original, nombre):
            continue
        que = "método" if _es_metodo(valor) else "atributo"
        anota(
            nombre,
            f"{que} '{nombre}' NO EXISTE en {original.__name__}. El doble "
            f"certifica algo que contra el original no puede pasar.",
        )

    # --- 2. firmas ----------------------------------------------------------
    # El doble tiene que tragarse TODA llamada que el original se traga. No al
    # revés: que acepte de más es inofensivo, que acepte de menos significa que
    # hay una forma de llamar que producción usa y el test no puede ejercitar.
    for nombre, valor in sorted(dbl.items()):
        if not _es_metodo(valor) or not hasattr(original, nombre):
            continue
        s_d, s_o = _firma(doble, nombre), _firma(original, nombre)
        if s_d is None or s_o is None:
            continue
        d, o = _Params(s_d), _Params(s_o)

        if not d.libre_pos and d.posicionales < o.posicionales:
            anota(
                nombre,
                f"'{nombre}{s_d}' acepta {d.posicionales} argumentos por "
                f"posición y el original acepta {o.posicionales} "
                f"('{nombre}{s_o}')",
            )
        if not d.libre_kw:
            # Los de solo-nombre que falten rompen SIEMPRE que se usen; los
            # posicionales renombrados solo rompen si se llaman por nombre.
            # Se separan para que el mensaje diga cuál de las dos cosas es.
            faltan = o.por_nombre - d.por_nombre
            graves = sorted(faltan & o.solo_nombre)
            leves = sorted(faltan - o.solo_nombre)
            if d.libre_pos:
                # Un doble con `*args` -`def get_hrv_data(self, *_)`- está
                # diciendo «me da igual lo que me pases por posición», y eso es
                # legítimo. Lo que `*args` NO absorbe son los de solo-nombre, y
                # ésos se siguen exigiendo abajo.
                leves = []
            if graves:
                anota(
                    nombre,
                    f"'{nombre}{s_d}' no acepta {graves}, que en el original "
                    f"son de solo-nombre ('{nombre}{s_o}'): esa llamada revienta",
                )
            if leves:
                anota(
                    nombre,
                    f"'{nombre}{s_d}' no acepta por nombre {leves}, que el "
                    f"original sí ('{nombre}{s_o}'). Normalmente es el "
                    f"parámetro traducido: ponle el nombre que tiene el original.",
                )

    # --- 3. campos anotados, cuando el original declara los suyos ------------
    # Éste es el caso del `ok` de `SendResult`, el que abrió todo esto: el doble
    # era un dataclass con un campo que el original no tiene, y el código de
    # producción lo leía con `getattr(envio, "ok", True)`.
    #
    # Se mira lo que SOBRA y no lo que falta, y la asimetría es deliberada:
    #
    #   falta un campo  -> `doble.loquesea` es un AttributeError. Ruidoso. Se
    #                      entera uno enseguida, y además un doble parcial -tres
    #                      campos de los doce- es normal y correcto.
    #   sobra un campo  -> nadie se entera nunca. El test se pone verde
    #                      certificando una rama que contra el original no se
    #                      puede recorrer. Es el defecto que hay que cerrar.
    #
    # Donde sí hace falta exigir el juego completo -`SesionFalsa` contra
    # `BuiltSession`, porque el cuerpo que sale a la red se construye de ahí-
    # hay un test dedicado que compara los campos Y SUS TIPOS uno a uno. Esa
    # exigencia es cara y no vale para todos; ésta es la que vale para todos.
    if declarados:
        anotados = {
            n for n in getattr(doble, "__annotations__", {}) if not n.startswith("_")
        }
        for n in sorted(anotados - declarados):
            if hasattr(original, n):
                continue
            anota(
                n,
                f"campo '{n}' no existe en {original.__name__}. Si producción "
                f"lo lee con `getattr(x, '{n}', algo)`, el test está probando "
                f"una rama inalcanzable.",
            )

    # --- 4. excepciones que ya no excusan nada -------------------------------
    # Un `salvo` que sobra es peor que inútil: alguien arregló la divergencia y
    # dejó puesta la exención, así que ese nombre se quedó fuera de la
    # comparación para siempre sin que nadie lo decidiera.
    #
    # Esta regla solo vale lo que valga `usadas`, y `usadas` se llena en `anota`
    # -al tapar algo concreto- y no al recorrer los nombres. La diferencia no es
    # cosmética: con lo segundo, un `salvo` sobre un método que el doble tiene y
    # que no diverge en nada contaba como usado y no se denunciaba jamás. Lo
    # cazó la mutación A6, no una lectura del código.
    for n in sorted(salvo - usadas):
        fuera.append(
            f"'{n}' está en `salvo` pero ya no diverge de {original.__name__}: "
            f"quítalo, o lo estarás dejando sin comparar para siempre"
        )

    return fuera


def estado_mutable_de_clase(cls) -> list[str]:
    """Atributos de clase que son `list`, `dict` o `set`.

    Un atributo de clase mutable lo comparten TODAS las instancias. En un doble
    que acumula llamadas -`self.escrituras.append(...)`- eso significa que el
    segundo test del módulo ve lo que hizo el primero, y que un test que pasa
    solo puede fallar en la batería entera o al revés. Y cuando el original
    declara ese mismo campo con `field(default_factory=list)`, el doble está
    modelando una cosa distinta de la que dice modelar.
    """
    return [
        f"'{n}' es un {type(v).__name__} de CLASE, compartido por todas las "
        f"instancias. Va en `__init__` o con `field(default_factory=...)`."
        for n, v in _publicos(cls).items()
        if isinstance(v, (list, dict, set))
    ]

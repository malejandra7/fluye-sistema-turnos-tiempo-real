// Utilidades compartidas por todas las pantallas de Fluye.

export async function api(ruta, { metodo = "GET", datos, pin } = {}) {
  const opciones = { method: metodo, headers: {} };
  if (datos !== undefined) {
    opciones.headers["Content-Type"] = "application/json";
    opciones.body = JSON.stringify(datos);
  }
  if (pin) opciones.headers["X-Pin"] = pin;
  const r = await fetch(ruta, opciones);
  const cuerpo = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const error = new Error(cuerpo?.error || cuerpo?.detail || "Algo salió mal. Intenta de nuevo.");
    error.estado = r.status;
    throw error;
  }
  return cuerpo;
}

// Canal en tiempo real con reconexión automática.
export function conectar(alMensaje, indicador) {
  let intentos = 0;
  function abrir() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => {
      intentos = 0;
      indicador?.classList.remove("caida");
      if (indicador) indicador.textContent = "En vivo";
      alMensaje({ t: "cambio" });
    };
    ws.onmessage = (e) => alMensaje(JSON.parse(e.data));
    ws.onclose = () => {
      indicador?.classList.add("caida");
      if (indicador) indicador.textContent = "Reconectando…";
      setTimeout(abrir, Math.min(10000, 500 * 2 ** intentos++));
    };
  }
  abrir();
}

// Agrupa avisos seguidos en una sola recarga.
export function enCola(fn, ms = 150) {
  let t;
  return () => { clearTimeout(t); t = setTimeout(fn, ms); };
}

export function avisar(texto, error = false) {
  const t = document.createElement("div");
  t.className = "toast" + (error ? " error" : "");
  t.setAttribute("role", error ? "alert" : "status");
  t.textContent = texto;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), error ? 5000 : 3000);
}

export function segundosDesde(iso) {
  return Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
}

export function reloj(segundos) {
  const s = Math.floor(segundos);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

export function duracion(minutos) {
  if (minutos === null || minutos === undefined) return "—";
  const m = Math.round(minutos);
  if (m < 1) return "menos de 1 min";
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim();
}

export function horaCorta(iso) {
  return new Date(iso).toLocaleTimeString("es-CO", { hour: "2-digit", minute: "2-digit" });
}

export function esc(texto) {
  return String(texto ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Sonido corto sin archivos: dos notas con Web Audio.
let audio;
export function campana(notas = [660, 880]) {
  try {
    audio = audio || new AudioContext();
    notas.forEach((f, i) => {
      const o = audio.createOscillator();
      const g = audio.createGain();
      o.frequency.value = f;
      o.type = "sine";
      const t = audio.currentTime + i * 0.28;
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.35, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.6);
      o.connect(g).connect(audio.destination);
      o.start(t);
      o.stop(t + 0.65);
    });
  } catch { /* sin audio disponible */ }
}

export const NOMBRE_PAUSA = { desayuno: "Desayuno", almuerzo: "Almuerzo", activa: "Pausa activa" };
export const NOMBRE_ESTADO = {
  disponible: "Disponible", atendiendo: "Atendiendo", pausa: "En pausa", desconectado: "Fuera",
};

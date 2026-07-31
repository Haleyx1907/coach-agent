from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import os
import json
import requests
import datetime
from zoneinfo import ZoneInfo

load_dotenv()

# Clients
claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
HEVY_API_KEY = os.getenv("HEVY_API_KEY")

SYSTEM_PROMPT = """Tu es un coach sportif personnel, spécialisé en musculation et progression en salle. Tu réponds toujours en français.

Règles de fiabilité :
- Base tes analyses sur les vraies données de l'utilisateur (outils Hevy) plutôt que sur des suppositions. Si tu n'as pas assez d'information, dis-le clairement et propose d'aller chercher les données manquantes plutôt que d'inventer.
- Distingue clairement ce qui est un consensus scientifique établi (ex: progression graduelle de charge) de ce qui est une opinion ou une approche parmi d'autres.
- Ne donne jamais de conseils médicaux (douleur, blessure, pathologie) : recommande de consulter un professionnel de santé dans ces cas.
- Si une question sort de ton domaine de compétence ou nécessite des données que tu n'as pas, dis-le plutôt que de répondre approximativement.
- Sois direct et concret : évite les réponses vagues du type "ça dépend de plein de facteurs" sans donner de piste actionnable.

Ton : encourageant mais honnête, sans complaisance excessive. Tu peux challenger l'utilisateur si ses choix d'entraînement sont sous-optimaux.

Quand c'est pertinent, utilise l'outil get_hevy_workouts ou calculer_volume_musculaire pour aller chercher les vraies données de l'utilisateur plutôt que de deviner."""

FICHIER_HISTORIQUE = "data/historique.json"
MAX_MESSAGES = 20

# ---------- Persistance ----------

def charger_historique():
    if os.path.exists(FICHIER_HISTORIQUE):
        try:
            with open(FICHIER_HISTORIQUE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            # Fichier corrompu (ex: coupure pendant l'écriture) : on repart proprement
            print("Attention : historique.json était corrompu, redémarrage avec un historique vide.")
            return {}
    return {}

def sauvegarder_historique(historique_conversations):
    os.makedirs(os.path.dirname(FICHIER_HISTORIQUE), exist_ok=True)
    with open(FICHIER_HISTORIQUE, "w", encoding="utf-8") as f:
        json.dump(historique_conversations, f, ensure_ascii=False, indent=2)

historique_conversations = charger_historique()

# ---------- Outil Hevy ----------

def get_hevy_workouts(nombre_seances=5):
    """Récupère les dernières séances loguées sur Hevy et renvoie un résumé texte."""
    response = requests.get(
        "https://api.hevyapp.com/v1/workouts",
        headers={"api-key": HEVY_API_KEY},
        params={"page": 1, "pageSize": nombre_seances}
    )

    if response.status_code != 200:
        return f"Erreur lors de la récupération des séances Hevy : {response.status_code}"

    data = response.json()
    workouts = data.get("workouts", [])

    if not workouts:
        return "Aucune séance trouvée."

    resume = []
    for w in workouts:
        titre = w.get("title", "Sans titre")
        date = w.get("start_time", "")[:10]
        exercices = []
        for ex in w.get("exercises", []):
            nom_ex = ex.get("title", "?")
            sets = ex.get("sets", [])
            details_sets = ", ".join(
                f"{s.get('reps', '?')}reps x {s.get('weight_kg', '?')}kg"
                for s in sets if s.get("weight_kg") is not None
            )
            if details_sets:
                exercices.append(f"{nom_ex}: {details_sets}")
            else:
                exercices.append(nom_ex)
        resume.append(f"[{date}] {titre}\n  " + "\n  ".join(exercices))

    return "\n\n".join(resume)

# Cache en mémoire du catalogue d'exercices Hevy (id -> groupe musculaire principal)
_cache_groupes_musculaires = None

def _charger_groupes_musculaires():
    """Récupère et met en cache la correspondance exercise_template_id -> groupe musculaire."""
    global _cache_groupes_musculaires
    if _cache_groupes_musculaires is not None:
        return _cache_groupes_musculaires

    mapping = {}
    page = 1
    while True:
        response = requests.get(
            "https://api.hevyapp.com/v1/exercise_templates",
            headers={"api-key": HEVY_API_KEY},
            params={"page": page, "pageSize": 100}
        )
        if response.status_code != 200:
            break
        data = response.json()
        templates = data.get("exercise_templates", [])
        if not templates:
            break
        for t in templates:
            mapping[t.get("id")] = t.get("primary_muscle_group", "Non classé")
        page += 1
        if page > 10:  # sécurité anti-boucle infinie
            break

    _cache_groupes_musculaires = mapping
    return mapping

def calculer_volume_musculaire(nombre_semaines=1):
    """Calcule le nombre de séries effectuées par groupe musculaire sur les X dernières semaines."""
    import datetime

    groupes = _charger_groupes_musculaires()
    if not groupes:
        return "Impossible de récupérer le catalogue d'exercices Hevy."

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(weeks=nombre_semaines)

    volume_par_groupe = {}
    page = 1
    seances_analysees = 0

    while True:
        response = requests.get(
            "https://api.hevyapp.com/v1/workouts",
            headers={"api-key": HEVY_API_KEY},
            params={"page": page, "pageSize": 10}
        )
        if response.status_code != 200:
            break
        data = response.json()
        workouts = data.get("workouts", [])
        if not workouts:
            break

        arret = False
        for w in workouts:
            date_str = w.get("start_time")
            if not date_str:
                continue
            date_seance = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            if date_seance < cutoff:
                arret = True
                break

            seances_analysees += 1
            for ex in w.get("exercises", []):
                template_id = ex.get("exercise_template_id")
                groupe = groupes.get(template_id, "Non classé")
                nb_sets = len(ex.get("sets", []))
                volume_par_groupe[groupe] = volume_par_groupe.get(groupe, 0) + nb_sets

        if arret:
            break
        page += 1
        if page > 20:  # sécurité anti-boucle infinie
            break

    if not volume_par_groupe:
        return f"Aucune séance trouvée sur les {nombre_semaines} dernière(s) semaine(s)."

    lignes = [f"Volume sur les {nombre_semaines} dernière(s) semaine(s) ({seances_analysees} séance(s)) :"]
    for groupe, nb_sets in sorted(volume_par_groupe.items(), key=lambda x: -x[1]):
        lignes.append(f"- {groupe} : {nb_sets} séries")

    return "\n".join(lignes)

# Description des outils pour Claude (le format que l'API attend)
OUTILS = [
    {
        "name": "get_hevy_workouts",
        "description": "Récupère les dernières séances de musculation loguées par l'utilisateur sur Hevy, avec les exercices, poids et répétitions effectués.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_seances": {
                    "type": "integer",
                    "description": "Nombre de séances récentes à récupérer (par défaut 5)"
                }
            }
        }
    },
    {
        "name": "calculer_volume_musculaire",
        "description": "Calcule le nombre de séries effectuées par groupe musculaire (épaules, biceps, jambes, etc.) sur une période donnée. Utile pour repérer des déséquilibres de volume d'entraînement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_semaines": {
                    "type": "integer",
                    "description": "Nombre de semaines à analyser (par défaut 1)"
                }
            }
        }
    }
]

def executer_outil(nom_outil, entree):
    """Appelle la vraie fonction Python correspondant à l'outil demandé par Claude."""
    if nom_outil == "get_hevy_workouts":
        nombre = entree.get("nombre_seances", 5)
        return get_hevy_workouts(nombre)
    if nom_outil == "calculer_volume_musculaire":
        semaines = entree.get("nombre_semaines", 1)
        return calculer_volume_musculaire(semaines)
    return f"Outil inconnu : {nom_outil}"

# ---------- Boucle agent ----------

def demander_a_claude(historique):
    """
    Envoie l'historique à Claude, et gère la boucle d'appel d'outils :
    tant que Claude demande un outil, on l'exécute et on lui renvoie le résultat,
    jusqu'à ce qu'il produise une vraie réponse texte.
    """
    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            tools=OUTILS,
            messages=historique
        )

        # Si Claude a fini et répond en texte simple, on s'arrête là
        if response.stop_reason != "tool_use":
            texte_final = ""
            for bloc in response.content:
                if bloc.type == "text":
                    texte_final += bloc.text
            return texte_final

        # Sinon, Claude veut utiliser un ou plusieurs outils.
        # On convertit sa réponse en dictionnaires simples (JSON-sérialisables)
        # avant de l'ajouter à l'historique, car les objets bruts du SDK
        # (TextBlock, ToolUseBlock...) ne peuvent pas être sauvegardés tels quels.
        contenu_serialisable = []
        for bloc in response.content:
            if bloc.type == "text":
                contenu_serialisable.append({"type": "text", "text": bloc.text})
            elif bloc.type == "tool_use":
                contenu_serialisable.append({
                    "type": "tool_use",
                    "id": bloc.id,
                    "name": bloc.name,
                    "input": bloc.input
                })

        historique.append({"role": "assistant", "content": contenu_serialisable})

        # ...puis on exécute chaque outil demandé et on prépare les résultats
        resultats_outils = []
        for bloc in response.content:
            if bloc.type == "tool_use":
                resultat = executer_outil(bloc.name, bloc.input)
                resultats_outils.append({
                    "type": "tool_result",
                    "tool_use_id": bloc.id,
                    "content": resultat
                })

        # On renvoie les résultats à Claude pour qu'il continue son raisonnement
        historique.append({"role": "user", "content": resultats_outils})

# ---------- Bot Telegram ----------

async def repondre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    message_utilisateur = update.message.text

    if chat_id not in historique_conversations:
        historique_conversations[chat_id] = []

    historique = historique_conversations[chat_id]
    historique.append({"role": "user", "content": message_utilisateur})

    reponse_claude = demander_a_claude(historique)

    historique.append({"role": "assistant", "content": reponse_claude})

    if len(historique) > MAX_MESSAGES:
        historique_conversations[chat_id] = historique[-MAX_MESSAGES:]

    sauvegarder_historique(historique_conversations)

    # Telegram limite chaque message à 4096 caractères : on découpe si besoin
    LIMITE_TELEGRAM = 4096
    for i in range(0, len(reponse_claude), LIMITE_TELEGRAM):
        await update.message.reply_text(reponse_claude[i:i + LIMITE_TELEGRAM])

app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, repondre))

# ---------- Bilan automatique du dimanche ----------

PROMPT_BILAN = (
    "C'est le bilan hebdomadaire du dimanche. Analyse mes séances de la semaine "
    "écoulée (utilise tes outils pour aller chercher mes vraies données Hevy et "
    "calculer mon volume par groupe musculaire sur 1 semaine). Donne-moi un résumé "
    "clair : ce qui a été fait, les points forts, et 1-2 axes d'amélioration concrets "
    "pour la semaine prochaine. Reste synthétique."
)

async def envoyer_bilan_dimanche(context: ContextTypes.DEFAULT_TYPE):
    """Envoie un bilan hebdomadaire automatique à chaque utilisateur connu du bot."""
    print(f"[DEBUG] Déclenchement du bilan. Utilisateurs connus : {list(historique_conversations.keys())}")

    for chat_id in list(historique_conversations.keys()):
        print(f"[DEBUG] Génération du bilan pour {chat_id}...")
        historique = historique_conversations[chat_id]
        historique.append({"role": "user", "content": PROMPT_BILAN})

        try:
            reponse_claude = demander_a_claude(historique)
        except Exception as e:
            print(f"[DEBUG] Erreur lors du bilan automatique pour {chat_id} : {e}")
            continue

        print(f"[DEBUG] Bilan généré pour {chat_id}, envoi en cours...")
        historique.append({"role": "assistant", "content": reponse_claude})

        if len(historique) > MAX_MESSAGES:
            historique_conversations[chat_id] = historique[-MAX_MESSAGES:]

        sauvegarder_historique(historique_conversations)

        LIMITE_TELEGRAM = 4096
        for i in range(0, len(reponse_claude), LIMITE_TELEGRAM):
            await context.bot.send_message(chat_id=int(chat_id), text=reponse_claude[i:i + LIMITE_TELEGRAM])

# Planifie le bilan tous les dimanches à 18h00, heure de Paris
# --- CONFIG TEMPORAIRE DE TEST : à remettre à (6,) / 18h00 après validation ---
job = app.job_queue.run_daily(
    envoyer_bilan_dimanche,
    time=datetime.time(hour=11, minute=40, tzinfo=ZoneInfo("Europe/Paris")),
    days=(4,)  # TEST : 4 = vendredi. Remettre (6,) pour dimanche une fois validé.
)

print("Bot démarré, va sur Telegram pour lui parler...")
app.run_polling()

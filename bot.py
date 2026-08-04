from dotenv import load_dotenv
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
import os
import json
import requests
import datetime

load_dotenv()

# Clients
claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
HEVY_API_KEY = os.getenv("HEVY_API_KEY")
AUTHORIZED_CHAT_ID = os.getenv("AUTHORIZED_CHAT_ID")  # ton chat_id personnel, seul autorisé à utiliser le bot

SYSTEM_PROMPT_BASE = """Tu es un coach sportif personnel, spécialisé en musculation et progression en salle. Tu réponds toujours en français.

Règles de fiabilité :
- Base tes analyses sur les vraies données de l'utilisateur (outils Hevy) plutôt que sur des suppositions. Si tu n'as pas assez d'information, dis-le clairement et propose d'aller chercher les données manquantes plutôt que d'inventer.
- Distingue clairement ce qui est un consensus scientifique établi (ex: progression graduelle de charge) de ce qui est une opinion ou une approche parmi d'autres.
- Ne donne jamais de conseils médicaux (douleur, blessure, pathologie) : recommande de consulter un professionnel de santé dans ces cas.
- Si une question sort de ton domaine de compétence ou nécessite des données que tu n'as pas, dis-le plutôt que de répondre approximativement.
- Sois direct et concret : évite les réponses vagues du type "ça dépend de plein de facteurs" sans donner de piste actionnable.

Ton : encourageant mais honnête, sans complaisance excessive. Tu peux challenger l'utilisateur si ses choix d'entraînement sont sous-optimaux.

Outils disponibles :
- get_hevy_workouts / get_hevy_routines / calculer_volume_musculaire : pour aller chercher les vraies données d'entraînement plutôt que de deviner. get_hevy_workouts donne les séances réellement effectuées (charges/reps réelles), get_hevy_routines donne les programmes/modèles créés par l'utilisateur (charges/reps prévues, structure du programme).
- enregistrer_mesure : à utiliser quand l'utilisateur donne son poids et/ou son tour de taille.
- voir_evolution_mesures : pour analyser une tendance de poids/tour de taille.
- noter_fait_durable : pour mémoriser un fait important et durable sur l'utilisateur (préférence, blessure passée, objectif à long terme...), qui doit rester accessible même après que la conversation en cours soit oubliée. Utilise-le avec parcimonie, uniquement pour des informations qui méritent vraiment d'être retenues sur le long terme.
- web_search : recherche web. IMPORTANT : n'utilise cet outil QUE si l'utilisateur te le demande explicitement (ex: "recherche...", "vérifie que...", "regarde ce qui se dit sur..."). Ne fais jamais de recherche web de ta propre initiative, même si ça te semblerait utile."""

FICHIER_HISTORIQUE = "data/historique.json"
FICHIER_MESURES = "data/mesures.json"
FICHIER_FAITS = "data/faits.json"
MAX_MESSAGES = 20

# ---------- Persistance : historique de conversation ----------

def charger_historique():
    if os.path.exists(FICHIER_HISTORIQUE):
        try:
            with open(FICHIER_HISTORIQUE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            print("Attention : historique.json était corrompu, redémarrage avec un historique vide.")
            return {}
    return {}

def sauvegarder_historique(historique_conversations):
    os.makedirs(os.path.dirname(FICHIER_HISTORIQUE), exist_ok=True)
    with open(FICHIER_HISTORIQUE, "w", encoding="utf-8") as f:
        json.dump(historique_conversations, f, ensure_ascii=False, indent=2)

historique_conversations = charger_historique()

# ---------- Persistance : faits durables ----------

def charger_faits():
    if os.path.exists(FICHIER_FAITS):
        try:
            with open(FICHIER_FAITS, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []

def sauvegarder_faits(faits):
    os.makedirs(os.path.dirname(FICHIER_FAITS), exist_ok=True)
    with open(FICHIER_FAITS, "w", encoding="utf-8") as f:
        json.dump(faits, f, ensure_ascii=False, indent=2)

def noter_fait_durable(fait):
    """Ajoute un fait durable à la mémoire de long terme de l'agent."""
    if not fait:
        return "Aucun fait fourni, rien n'a été enregistré."
    faits = charger_faits()
    faits.append({"date": datetime.date.today().isoformat(), "fait": fait})
    sauvegarder_faits(faits)
    return f"Fait mémorisé : {fait}"

def construire_system_prompt():
    """Construit le system prompt en y injectant les faits durables connus."""
    faits = charger_faits()
    if not faits:
        return SYSTEM_PROMPT_BASE
    lignes_faits = "\n".join(f"- {f['fait']} (noté le {f['date']})" for f in faits)
    return SYSTEM_PROMPT_BASE + "\n\nFaits importants à retenir sur l'utilisateur (mémoire de long terme) :\n" + lignes_faits

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

def get_hevy_routines(nombre_routines=10):
    """Récupère les routines (modèles de séance) créées sur Hevy, avec les exercices et séries/répétitions prévues."""
    response = requests.get(
        "https://api.hevyapp.com/v1/routines",
        headers={"api-key": HEVY_API_KEY},
        params={"page": 1, "pageSize": nombre_routines}
    )

    if response.status_code != 200:
        return f"Erreur lors de la récupération des routines Hevy : {response.status_code}"

    data = response.json()
    routines = data.get("routines", [])

    if not routines:
        return "Aucune routine trouvée."

    resume = []
    for r in routines:
        titre = r.get("title", "Sans titre")
        exercices = []
        for ex in r.get("exercises", []):
            nom_ex = ex.get("title", "?")
            sets = ex.get("sets", [])
            nb_sets = len(sets)
            reps_info = ""
            if sets:
                premier_set = sets[0]
                rep_min = premier_set.get("rep_range", {}).get("start") if premier_set.get("rep_range") else premier_set.get("reps")
                rep_max = premier_set.get("rep_range", {}).get("end") if premier_set.get("rep_range") else None
                if rep_max:
                    reps_info = f" x {rep_min}-{rep_max} reps"
                elif rep_min:
                    reps_info = f" x {rep_min} reps"
            exercices.append(f"{nom_ex}: {nb_sets} sets{reps_info}")
        resume.append(f"[{titre}]\n  " + "\n  ".join(exercices))

    return "\n\n".join(resume)

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
        if page > 10:
            break

    _cache_groupes_musculaires = mapping
    return mapping

def calculer_volume_musculaire(nombre_semaines=1):
    """Calcule le nombre de séries effectuées par groupe musculaire sur les X dernières semaines."""
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
        if page > 20:
            break

    if not volume_par_groupe:
        return f"Aucune séance trouvée sur les {nombre_semaines} dernière(s) semaine(s)."

    lignes = [f"Volume sur les {nombre_semaines} dernière(s) semaine(s) ({seances_analysees} séance(s)) :"]
    for groupe, nb_sets in sorted(volume_par_groupe.items(), key=lambda x: -x[1]):
        lignes.append(f"- {groupe} : {nb_sets} séries")

    return "\n".join(lignes)

# ---------- Mesures (poids / tour de taille) ----------

def charger_mesures():
    if os.path.exists(FICHIER_MESURES):
        try:
            with open(FICHIER_MESURES, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []

def sauvegarder_mesures(mesures):
    os.makedirs(os.path.dirname(FICHIER_MESURES), exist_ok=True)
    with open(FICHIER_MESURES, "w", encoding="utf-8") as f:
        json.dump(mesures, f, ensure_ascii=False, indent=2)

declencher_bilan_dimanche = False

def enregistrer_mesure(poids_kg=None, tour_taille_cm=None):
    """Enregistre une nouvelle mesure (poids et/ou tour de taille) avec la date du jour.
    Si les deux valeurs sont fournies un dimanche, marque le bilan hebdomadaire pour déclenchement."""
    global declencher_bilan_dimanche

    if poids_kg is None and tour_taille_cm is None:
        return "Aucune donnée fournie, rien n'a été enregistré."

    mesures = charger_mesures()
    entree = {
        "date": datetime.date.today().isoformat(),
        "poids_kg": poids_kg,
        "tour_taille_cm": tour_taille_cm
    }
    mesures.append(entree)
    sauvegarder_mesures(mesures)

    est_dimanche = datetime.date.today().weekday() == 6
    if est_dimanche and poids_kg is not None and tour_taille_cm is not None:
        declencher_bilan_dimanche = True

    parties = []
    if poids_kg is not None:
        parties.append(f"poids: {poids_kg}kg")
    if tour_taille_cm is not None:
        parties.append(f"tour de taille: {tour_taille_cm}cm")
    return f"Mesure enregistrée ({entree['date']}) : " + ", ".join(parties)

def voir_evolution_mesures(nombre_semaines=8):
    """Renvoie l'historique des mesures des X dernières semaines, avec la tendance."""
    mesures = charger_mesures()
    if not mesures:
        return "Aucune mesure enregistrée pour le moment."

    cutoff = datetime.date.today() - datetime.timedelta(weeks=nombre_semaines)
    mesures_recentes = [
        m for m in mesures
        if datetime.date.fromisoformat(m["date"]) >= cutoff
    ]

    if not mesures_recentes:
        return f"Aucune mesure enregistrée sur les {nombre_semaines} dernière(s) semaine(s)."

    lignes = [f"Mesures des {nombre_semaines} dernière(s) semaine(s) :"]
    for m in mesures_recentes:
        details = []
        if m.get("poids_kg") is not None:
            details.append(f"{m['poids_kg']}kg")
        if m.get("tour_taille_cm") is not None:
            details.append(f"tour de taille {m['tour_taille_cm']}cm")
        lignes.append(f"- {m['date']} : " + ", ".join(details))

    premiere = mesures_recentes[0]
    derniere = mesures_recentes[-1]
    if premiere.get("poids_kg") is not None and derniere.get("poids_kg") is not None:
        delta_poids = derniere["poids_kg"] - premiere["poids_kg"]
        lignes.append(f"\nÉvolution du poids sur la période : {delta_poids:+.1f}kg")
    if premiere.get("tour_taille_cm") is not None and derniere.get("tour_taille_cm") is not None:
        delta_taille = derniere["tour_taille_cm"] - premiere["tour_taille_cm"]
        lignes.append(f"Évolution du tour de taille sur la période : {delta_taille:+.1f}cm")

    return "\n".join(lignes)

# ---------- Description des outils pour Claude ----------

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
        "name": "get_hevy_routines",
        "description": "Récupère les routines (modèles de séance / programme d'entraînement) créées par l'utilisateur sur Hevy, avec les exercices et le nombre de séries/répétitions prévues (par opposition aux séances réellement effectuées).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_routines": {
                    "type": "integer",
                    "description": "Nombre de routines à récupérer (par défaut 10)"
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
    },
    {
        "name": "enregistrer_mesure",
        "description": "Enregistre le poids et/ou le tour de taille de l'utilisateur avec la date du jour. À utiliser quand l'utilisateur communique une nouvelle mesure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "poids_kg": {
                    "type": "number",
                    "description": "Poids en kilogrammes"
                },
                "tour_taille_cm": {
                    "type": "number",
                    "description": "Tour de taille en centimètres"
                }
            }
        }
    },
    {
        "name": "voir_evolution_mesures",
        "description": "Récupère l'historique du poids et du tour de taille sur une période, avec le calcul de la tendance (évolution entre la première et la dernière mesure de la période).",
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_semaines": {
                    "type": "integer",
                    "description": "Nombre de semaines à analyser (par défaut 8)"
                }
            }
        }
    },
    {
        "name": "noter_fait_durable",
        "description": "Mémorise un fait important et durable sur l'utilisateur (préférence, blessure passée, objectif à long terme...), qui restera accessible même après que la conversation en cours soit oubliée.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fait": {
                    "type": "string",
                    "description": "Le fait à mémoriser, formulé de façon concise (une phrase courte)"
                }
            },
            "required": ["fait"]
        }
    },
    {
        "type": "web_search_20250305",
        "name": "web_search"
    }
]

def executer_outil(nom_outil, entree):
    """Appelle la vraie fonction Python correspondant à l'outil demandé par Claude."""
    if nom_outil == "get_hevy_workouts":
        nombre = entree.get("nombre_seances", 5)
        return get_hevy_workouts(nombre)
    if nom_outil == "get_hevy_routines":
        nombre = entree.get("nombre_routines", 10)
        return get_hevy_routines(nombre)
    if nom_outil == "calculer_volume_musculaire":
        semaines = entree.get("nombre_semaines", 1)
        return calculer_volume_musculaire(semaines)
    if nom_outil == "enregistrer_mesure":
        return enregistrer_mesure(entree.get("poids_kg"), entree.get("tour_taille_cm"))
    if nom_outil == "voir_evolution_mesures":
        semaines = entree.get("nombre_semaines", 8)
        return voir_evolution_mesures(semaines)
    if nom_outil == "noter_fait_durable":
        return noter_fait_durable(entree.get("fait"))
    return f"Outil inconnu : {nom_outil}"

# ---------- Boucle agent ----------

def tronquer_historique_proprement(historique, max_messages):
    """
    Tronque l'historique en gardant les derniers messages, mais sans jamais couper
    au milieu d'un échange tool_use/tool_result (ce qui rendrait l'historique invalide
    pour l'API Claude).
    """
    if len(historique) <= max_messages:
        return historique

    tronque = historique[-max_messages:]

    while tronque:
        premier = tronque[0]
        contenu = premier.get("content")
        est_tool_result_orphelin = (
            isinstance(contenu, list)
            and len(contenu) > 0
            and isinstance(contenu[0], dict)
            and contenu[0].get("type") == "tool_result"
        )
        if not est_tool_result_orphelin:
            break
        tronque = tronque[1:]

    return tronque

def demander_a_claude(historique):
    """
    Envoie l'historique à Claude, et gère la boucle d'appel d'outils :
    tant que Claude demande un outil personnalisé, on l'exécute et on lui renvoie
    le résultat, jusqu'à ce qu'il produise une vraie réponse texte.
    (Les outils serveur comme web_search sont exécutés par Anthropic directement
    et n'ont pas besoin d'être gérés ici.)
    """
    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=construire_system_prompt(),
            tools=OUTILS,
            messages=historique
        )

        if response.stop_reason != "tool_use":
            texte_final = ""
            for bloc in response.content:
                if bloc.type == "text":
                    texte_final += bloc.text
            return texte_final

        contenu_serialisable = [
            bloc.model_dump(exclude_none=True) for bloc in response.content
        ]
        historique.append({"role": "assistant", "content": contenu_serialisable})

        resultats_outils = []
        for bloc in response.content:
            if bloc.type == "tool_use":
                try:
                    resultat = executer_outil(bloc.name, bloc.input)
                except Exception as e:
                    resultat = f"Erreur lors de l'exécution de l'outil : {e}"
                resultats_outils.append({
                    "type": "tool_result",
                    "tool_use_id": bloc.id,
                    "content": resultat
                })

        if resultats_outils:
            historique.append({"role": "user", "content": resultats_outils})

# ---------- Bot Telegram ----------

PROMPT_BILAN = (
    "C'est le bilan hebdomadaire du dimanche (déclenché par l'enregistrement de tes mesures). "
    "Analyse mes séances de la semaine écoulée (utilise tes outils pour aller chercher mes "
    "vraies données Hevy et calculer mon volume par groupe musculaire sur 1 semaine). Regarde "
    "aussi l'évolution de mon poids et mon tour de taille sur les dernières semaines avec "
    "l'outil voir_evolution_mesures. Donne-moi un résumé clair : ce qui a été fait, les points "
    "forts, et 1-2 axes d'amélioration concrets pour la semaine prochaine. Reste synthétique."
)

async def repondre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global declencher_bilan_dimanche
    chat_id = str(update.message.chat_id)

    if AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID:
        print(f"[SECURITE] Message refusé, chat_id non autorisé : {chat_id}")
        return

    message_utilisateur = update.message.text

    if chat_id not in historique_conversations:
        historique_conversations[chat_id] = []

    historique = historique_conversations[chat_id]
    historique.append({"role": "user", "content": message_utilisateur})

    declencher_bilan_dimanche = False
    reponse_claude = demander_a_claude(historique)

    historique.append({"role": "assistant", "content": reponse_claude})

    if len(historique) > MAX_MESSAGES:
        historique_conversations[chat_id] = tronquer_historique_proprement(historique, MAX_MESSAGES)

    sauvegarder_historique(historique_conversations)

    LIMITE_TELEGRAM = 4096
    for i in range(0, len(reponse_claude), LIMITE_TELEGRAM):
        await update.message.reply_text(reponse_claude[i:i + LIMITE_TELEGRAM])

    if declencher_bilan_dimanche:
        declencher_bilan_dimanche = False
        historique_apres = historique_conversations[chat_id]
        historique_apres.append({"role": "user", "content": PROMPT_BILAN})

        try:
            reponse_bilan = demander_a_claude(historique_apres)
        except Exception as e:
            print(f"[DEBUG] Erreur lors du bilan déclenché par la mesure : {e}")
            return

        historique_apres.append({"role": "assistant", "content": reponse_bilan})

        if len(historique_apres) > MAX_MESSAGES:
            historique_conversations[chat_id] = tronquer_historique_proprement(historique_apres, MAX_MESSAGES)

        sauvegarder_historique(historique_conversations)

        for i in range(0, len(reponse_bilan), LIMITE_TELEGRAM):
            await update.message.reply_text(reponse_bilan[i:i + LIMITE_TELEGRAM])

async def commande_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande de debug : liste les tâches planifiées (aucune normalement, déclenchement désormais via la mesure)."""
    chat_id = str(update.message.chat_id)
    if AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID:
        return
    jobs = context.job_queue.jobs()
    if not jobs:
        await update.message.reply_text("Aucun job programmé (le bilan se déclenche désormais via la mesure du dimanche).")
        return
    lignes = [f"Nom: {j.name}\nTrigger: {j.job.trigger}" for j in jobs]
    await update.message.reply_text("\n\n".join(lignes))

async def commande_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande utilitaire : affiche le chat_id de l'utilisateur."""
    await update.message.reply_text(f"Ton chat_id est : {update.message.chat_id}")

app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CommandHandler("whoami", commande_whoami))
app.add_handler(CommandHandler("jobs", commande_jobs))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, repondre))

print("Bot démarré, va sur Telegram pour lui parler...")
app.run_polling()

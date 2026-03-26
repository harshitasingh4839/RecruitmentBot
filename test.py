import asyncio
from agent import (
    run_candidate_scheduler_agent,
    resolve_candidate_scheduling_session_tool,
)

async def main():
    context = {
        "candidateId": "cand_001",
        "recruiterEmail": "harshita.singh@foyr.com",
        "jobId": "jd_001",
        "jobTitle": "Data Scientist",
        "provider": "google",
        "timezone": "Asia/Kolkata",
        "mode": "google_meet",
        "activeSessionId": None,
        "scheduledInterviewId": None,
    }

    history = []

    print("Candidate Scheduling Agent Test")
    print("Type 'exit' to stop.\n")

    # First outbound bot message using resolveCandidateSchedulingSession
    initial = await resolve_candidate_scheduling_session_tool({}, context)

    base_intro = (
        f"Hi! You’ve been shortlisted for the {context['jobTitle']} role "
        f"(Job ID: {context['jobId']})."
    )

    message_text = initial.get("messageText") or "I’m here to help you with your interview scheduling."
    next_action = initial.get("nextAction")

    if next_action in {"new_session_created", "continue_session"}:
        full_opening = (
            f"{base_intro} "
            "Please schedule your interview by choosing one of the available slots below.\n\n"
            f"{message_text}"
        )
    elif next_action == "already_scheduled":
        full_opening = f"{base_intro} {message_text}"
    else:
        full_opening = f"{base_intro}\n\n{message_text}"

    history.append({"role": "assistant", "content": full_opening})

    print(f"Bot:\n{full_opening}\n")
    print("-" * 80)

    while True:
        user_text = input("Candidate: ").strip()
        if user_text.lower() in {"exit", "quit"}:
            print("\nBot:\nGoodbye!")
            break

        if not user_text:
            continue

        reply, history, context = await run_candidate_scheduler_agent(
            user_message=user_text,
            conversation_history=history,
            context=context,
        )

        print(f"\nBot:\n{reply}\n")
        print("Context:", context)
        print("-" * 80)

if __name__ == "__main__":
    asyncio.run(main())
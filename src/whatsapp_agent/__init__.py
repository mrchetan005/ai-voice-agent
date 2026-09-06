"""WhatsApp agent: a general WhatsApp assistant (chat + voice calls) built
on the voiceagent library. Appointment booking is its first capability;
new capabilities are sibling packages under capabilities/.

Entry points: the `whatsapp-agent` CLI (serve/call/inbound/chat/audit) and
the FastAPI factory `whatsapp_agent.api.app:create_app`.
"""

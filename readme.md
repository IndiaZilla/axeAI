    [note for you. yes, YOU. this project is currently in development. The ideas, the logic etc. are entirely made by ME(definetly a hoo-man and not an AI). This may be unpolished, and there may be edge cases not accounted for, but it works. thanks!!!!!!!!!!!!!]    
    
    Hello, fellow developer! Welcome to axeAI!

    axeAI:

    a rchitecture for e-
    x treme
    e xploitation of
    A rtificial
    I ntelligence


    axeAI is an architecture meant to help solve the current bottlenecks(context drift, lack of focus, communication breakdowns, endless hallucination loops etc.) in parallel-operating agentic AI.
    

    Currently, it supports Google AI Studio API Keys, and uses the 'gemini-3.6-flash' and 'gemini-3.5-flash-lite' models.
    
    3.6-flash is smart and not a wallet burner(58.7% on SWE‑Bench Pro, 49% on DeepSWE, 63.9% on MLE‑Bench, 83% on OSWorld‑Verified, and 1421 Elo on GDPval‑AA v2 with 17% fewer tokens on Artificial Analysis Index, up to 65% fewer output tokens on DeepSWE tasks, and a lower output token cost: $7.50 per million tokens (down from $9)..), 
    
    while Flash-lite also has a good reasoning but more importantly much cheaper($0.30 per 1M input tokens, and $2.50 per 1M output tokens), making it an optimum choice for the agents.

    {sorry if this is turning into a bit of a Gemini API ad; just justifiying my choice of models}
    
    It will be expanded to support configurable AI models and various providers, along with local processing.
    [Check: FUTURE FEATURES]


    MAJOR FEATURES(current):

    -> Organised multi-level chain-of-command structure
        Has a top level orchestrator agent('S') that takes instruction directly from the user and divides it between all the agents.
    -> JSON enforced prompt structuring
        Forces the AI to respond in a fixed JSON structure for increased efficiency     
    -> Dynamic Context via 'AI Scratchpad'
        Allows the AI to note down context in a 'scratchpad', letting it focus on completing its goals without encurring context drift.
    -> Context-cacheing support
        The prompt structure supports automatic context-cacheing/prompt-cacheing(reducing costs and latency).
    -> Parallel Debate mode(Board Room)
        Allows any agent to request 'S' to trigger a 'Board Room' discussion where all the agents can discuss and debate.
    -> Multiple Review Protocol
        Features a review by the orchestrator every 5 iteration cycles but also allows the worker to trigger it on-demand.

    {there are other current features too, please explore the code for the same; they will soon be featured here}


    HOW TO RUN:
    step 1) Install the repo
    step 2) Run a0.py in the respective terminal of your OS(eg for windows, run python a0.py) 
    
    HOW IT WORKS:

    its easier for me to explain via a simple flowchart/diagram.

                                                                        |---> a1.py(file for 'S', the orchestr)
    user ----"task prompt"----> a0(underlying system) ---- initialise --|---> comm_hub.py(the communication highway for the agents)
                                                                        |---> context_hub.py(the context storage for the agents) 
                                                                    
    S(via a1) ---- "roles and prompts" ----> a0 ---- "bootup agents" ----> aG

    {note 2 self: add rest later}


    {note 2 self: add the missing stuffs}
pls don't mind the errors
shashwat out







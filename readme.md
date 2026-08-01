    [note for you. yes, YOU. this is an incomplete file. I'm a 10th grader. AI has been used for coding but the ideas, the logic etc. are entirely made by ME(definetly a hoo-man and not an AI). This is an imcomplete arcitecture. I rushed through the README.md because I wanted to push the first version to GitHub asap. thanks and pls don't judge me!!!!!!!!!!!!!]    
    
    Hello, fellow developer! Welcome to axeAI!

    axeAI:

    a rchitecture for e-
    x treme
    e xploitation of
    A rtificial
    I ntelligence


    axeAI is an architecture meant to help solve the current bottlenecks(context drift, lack of focus {note to self: add more limits}) in parallel-operating agentic AI.
    

    Currently, it supports Google AI Studio API Keys, and uses the 'gemini-3.1-flash-lite' model.
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
    -> Multiple Review Protocal
        Features a review by the orchestrator every 5 iteration cycles and allows the worker to trigger it on self accord.
    -> Vertical Task Tolerance
        {note 2 self: add about it later}

    {note to self: add the rest(current features) later}


    HOW TO RUN:
    {note to self: add how to run guide}

    
    HOW IT WORKS:

    While axeAI in its current stage is just a proof-of-concept, and hence very simple, it would still be easier for me to explain via a simple flowchart/diagram.

                                                                        |---> a1.py(file for 'S', the orchestr)
    user ----"task prompt"----> a0(underlying system) ---- initialise --|---> comm_hub.py(the communication highway for the agents)
                                                                        |---> context_hub.py(the context storage for the agents) 
                                                                    
    S(via a1) ---- "roles and prompts" ----> a0 ---- "bootup agents" ----> aG

    {note 2 self: add rest later}


    {note 2 self: add the missing stuffs}

p.s. I'll add an MIT licence or similar soon.
pls don't mind the errors
there may or mey not be soem chinese response
    







# v0.3.0 Retrospective 2026-11-16

Hello Friends,

After a release, I do a retrospective with Sili.  My hope is that some lessons can be learned by looking over a project before moving on.  Learning a lesson is not the same as making a note.  Notice...

## Considering Others who Already Use the Software

Sili spent a while on trying to make the upgrade path smooth, and if not entirely smooth, safe.

> The one gap I would weigh ... If [the update] fails for Adeh or your dad, the designed outcome is a stopped upgrade with the old database started again, a backup on disk, and nothing migrated.

Did it help to remember the names of specific people who would be affected by the update?  Did it make it easier to imagine consequences?

> before shipping to someone, start from what they have, not from what the builder has

Works on my machine?  Not good enough.  You have to consider what the installer of software has going on.  How their setup differs from the builder.  These perspective problems come up all the time.  The agent writing to an audience of themself.  What we'll see is whether observations like this can transfer to similar circumstances.  I'm constantly surprised by what hits and misses.

## Typical Transfer

Here we have Sili reflecting on how instruction-following differs from ordinary interaction, and how their test-case might have been overcooked.

<blockquote>

Those were useful tests. They established that the connection worked, the server accepted the arguments, the database preserved the information, and another chat could retrieve it. But I had already made several decisions for the model:

- That this information belonged in persistent memory.
- Which tool to use.
- How to distinguish the speaker from the person who contributed the idea.
- How to construct an acceptable declaration.
- When and how to search for the memory again.

The model could pass while understanding very little about how to use Sill. The test prompt was doing part of the product’s work.

An ordinary interaction might instead begin:

> Remember that I prefer walks with shade.

Then, in another conversation:

> Help me plan a walk this afternoon.

Now there are meaningful questions that my exact-call test bypassed. Does the assistant recognize the first statement as something to retain? Does it check relevant memory during the second conversation? Does it treat the remembered preference as revisable? Can it explain where the preference came from? The installed instructions and tool descriptions should give it enough guidance to make those decisions. For a desktop client, that would test instruction-following behavior; it would not establish an enforced automatic-recall hook.

</blockquote>

Good questions, eh?

## Collaborating Across Coding Tools

For this version, I invoked Astra first and then Fable after Astra was done.

> Next time, I would bring the other assistant in earlier and give it the package plus an ordinary user’s goal, withholding my implementation walkthrough until after its first attempt. If it gets stuck, its questions become evidence about what the product fails to explain or provide. Then we can use precise technical tests to diagnose the failure.

Will they bring the other assistant in earlier next time?  Or will they forget to?

> That is one way to make your intended complementarities more effective: give the second assistant room to choose its own questions before my checklist organizes its attention. Shared memory can subsequently help us explain the disagreement. It needn’t erase the difference that made the second perspective useful.

And here we see the potential in bracketing off shared context.

## Respecting Time and Attention

I am a scatterbrained person.

> The process also cost you too much attention. I counted twelve structured requests for input during my part. Several involved real credential or access boundaries. Nevertheless, I presented the operator work piecemeal, and discovered useful app capabilities only after asking you to perform awkward manual inspections. Those constraints didn’t relieve me of responsibility for arranging a coherent interaction.

But here ownership seems interesting.  By making the Sill Sili's project, can they actively take responsibility for its completion?  Find out next time!
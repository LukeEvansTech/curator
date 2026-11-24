# Glossary

The words the pages, the API and the phone app use, each with one meaning.

| Term            | Meaning                                                                                                                |
| --------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Library         | Films (`film`, held by Radarr) or TV (`tv`, held by Sonarr). Each has its own runs, lists and counts.                  |
| Service         | Radarr or Sonarr: where a library's titles and tags live.                                                              |
| Engine          | The scheduled jobs outside this app that plan, judge and delete.                                                       |
| Run             | One weekly pass of the engine over a library.                                                                          |
| Review document | What a run publishes here: the lists below, as they stood when the run finished.                                       |
| This week       | The latest review document of each library, with the tags as they are now.                                             |
| Judge           | The model that proposes keep or delete for a title, with a reason.                                                     |
| Verdict         | One proposal by the judge, or by a person, for one title.                                                              |
| Going           | Titles the next run deletes unless somebody keeps them.                                                                |
| Later           | Titles proposed for deletion that are not yet due, or held back by the cap.                                            |
| Needs you       | Titles the engine could not assess. Nothing happens to them until a person decides.                                    |
| Kept for a year | Titles the judge chose to keep. It judges them again after a year.                                                     |
| Due             | The moment a proposal may be carried out: a week after it was made.                                                    |
| Cap             | The most titles one run deletes from a library.                                                                        |
| Dry run         | A run that deletes nothing and says what it would have done.                                                           |
| Keep            | A person's press that adds `cleanup-keep`: no run deletes the title. On a kept-for-a-year title it reads Keep forever. |
| Delete instead  | A person's press that adds `cleanup-eligible`: the next run deletes the title.                                         |
| Undo            | Takes back a tag this app added, or a mark. A tag set elsewhere is never undone here.                                  |
| Mark            | An answer that changes no tag: Let it go or Decide later. A mark never holds a title back.                             |
| Let it go       | A mark: seen, and left as the list has it. A title in Going still goes.                                                |
| Decide later    | A mark: put off to the end of the list. It lasts for the document it was made on.                                      |
| Stand           | Where a title stands now: waiting, kept, marked for deletion, let go, put off, or sent and not yet shown.              |
| Sent            | A tag the service accepted and has not shown yet.                                                                      |
| Kept by you     | Titles on the Going list that carry `cleanup-keep` since the document was published.                                   |
| Review          | Going through a list one title at a time. The lists are Due at the next run, Later and Being kept for a year.          |
| Deleted         | What the runs took, largest first.                                                                                     |
| Recycle bin     | Where a service holds a deleted file for a set number of days before it is gone for good.                              |
| Allow list      | Titles no run may propose: above the rating bar, or tagged `keep` or `cleanup-keep`.                                   |
| Notice          | A warning above a list: a dry run, an old list, a blocked run, or tags that could not be read.                         |

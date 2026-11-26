# Policies

A `Policy` is a `Descriptor` plus a value. In these folders, each policy is a setting:
a requirement that binds the reader of the list. This folder only holds one folder for
each place of a list of settings.

## Rules for all the settings

- A setting is identified only by the exact bytes of its formal. The tags and the
  prose are for a reader. A policy with an empty formal is not valid.
- A node that does not know the formal of a setting, does not understand its value,
  or finds it in a place where it is not valid, refuses (it does not run the service,
  or it refuses the start). The answer gives the descriptor of the setting (REASON
  3.7).
- In one list, two policies do not have the same formal. A sender sorts the list by
  formal, ascending. A reader refuses a repeated formal.
- A setting fails closed, so give each one a default for its absence that is safe on
  an old node.
- The formal gives `place`: the fields where the setting is valid. A setting that is
  valid in two places is in the folder of its main place.

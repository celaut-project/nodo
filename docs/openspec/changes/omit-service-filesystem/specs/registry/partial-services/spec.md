# Spec Delta

## Purpose
Define how a node stores a service without its filesystem, how it marks such a service, and
what the node can and cannot do with it. This lets a small device know services that are too
large to store.

## ADDED Requirements

### Requirement: Omit the filesystem on acquisition
When `misc.OMIT_FS` is `true`, the node SHALL NOT keep the filesystem of a service that it
imports, downloads or gets from a peer or from a source. The node SHALL keep the
specification and the metadata, and SHALL mark the service as partial. A service that has no
filesystem block (its filesystem is in its specification) SHALL stay complete. When the
option is `false`, the node keeps complete services.

#### Scenario: Import with the option on
- **GIVEN** `misc.OMIT_FS` is `true`
- **WHEN** the operator imports a service
- **THEN** the registry holds the specification of the service and the marker `.nofs`
- **AND** the filesystem blocks of the service are not on the disk of the node

#### Scenario: Import with the option off
- **GIVEN** `misc.OMIT_FS` is `false`
- **WHEN** the operator imports a service
- **THEN** the registry holds the complete service and no marker

#### Scenario: A service with no filesystem block
- **GIVEN** `misc.OMIT_FS` is `true`
- **WHEN** the operator imports a service that has no block
- **THEN** the registry holds the complete service and no marker

#### Scenario: A service that the node holds in full
- **GIVEN** the node holds a service in full and `misc.OMIT_FS` is `true`
- **WHEN** the operator imports the same service again
- **THEN** the service stays complete

### Requirement: Verify the service id when the filesystem is omitted
When `misc.VALIDATE_ON_IMPORT` is on, the node SHALL read the complete service, including the
filesystem, and SHALL compare its hash with the requested id before it keeps anything. The
node SHALL NOT store the filesystem blocks. If the id is not the same, the node SHALL refuse
the service and keep nothing of it.

#### Scenario: Valid service
- **GIVEN** `misc.OMIT_FS` and `misc.VALIDATE_ON_IMPORT` are `true`
- **WHEN** the node acquires a service whose content has the requested id
- **THEN** the node keeps the service as partial
- **AND** the filesystem is not left on disk when the acquisition ends

#### Scenario: Wrong id
- **GIVEN** `misc.OMIT_FS` and `misc.VALIDATE_ON_IMPORT` are `true`
- **WHEN** the content of the service does not have the requested id
- **THEN** the node refuses the service and keeps no block of it

### Requirement: Skip the filesystem when the id is not verified
When `misc.OMIT_FS` is `true` and `misc.VALIDATE_ON_IMPORT` is off, the node SHALL NOT receive
the filesystem block from the sender. The node SHALL keep the service as partial. The node
cannot check the id of the content that it did not receive, and SHALL say this to the operator.

#### Scenario: Acquisition from a peer without verification
- **GIVEN** `misc.OMIT_FS` is `true` and `misc.VALIDATE_ON_IMPORT` is `false`
- **WHEN** the node gets a service from a peer
- **THEN** the peer does not send the filesystem body
- **AND** the node keeps the service as partial
- **AND** the node tells the operator that the id was not verified over the filesystem

### Requirement: Mark a partial service
A partial service SHALL have an empty file `.nofs` next to `_.json` in its registry entry. A
service without this file SHALL be complete. The node SHALL treat the marker as the only
source of the partial state.

#### Scenario: Read the state
- **WHEN** the node reads the state of a service
- **THEN** the service is partial if `.nofs` exists, and complete if it does not

### Requirement: Do not run a partial service locally
The node SHALL NOT start a partial service on itself. The node SHALL NOT offer itself as a
local candidate for it. The node SHALL still price the service from its specification and
SHALL still delegate it to a peer that holds the service in full. The refusal to run locally
SHALL appear in the launch failure when no peer can run the service.

#### Scenario: Start with a peer that has the service
- **GIVEN** the node holds a service as partial
- **AND** a connected peer holds it in full
- **WHEN** a client asks the node to start the service
- **THEN** the node delegates the start to the peer

#### Scenario: Start with no peer that can run it
- **GIVEN** the node holds a service as partial and no peer can run it
- **WHEN** a client asks the node to start the service
- **THEN** the start fails
- **AND** the error says that the service is partial and cannot run on this node

### Requirement: Do not give a partial service to a peer
The node SHALL NOT send a partial service to a peer that asks for it with `GetService`. The
node SHALL answer as if it does not hold the service in full. The node SHALL NOT export or
publish a partial service, and the command SHALL say why.

#### Scenario: A peer asks for a partial service
- **GIVEN** the node holds a service as partial
- **WHEN** a peer calls `GetService` for it
- **THEN** the node does not send the service

#### Scenario: Export of a partial service
- **GIVEN** the node holds a service as partial
- **WHEN** the operator exports or publishes it
- **THEN** the command fails and says that the service is partial

### Requirement: Download completes a partial service
`nodo download` SHALL NOT treat a partial service as a service that the node already holds.
When `misc.OMIT_FS` is `false`, the command SHALL get the full service, check its id, and
remove `.nofs`. When `misc.OMIT_FS` is `true`, the service stays partial.

#### Scenario: Complete a partial service
- **GIVEN** the node holds a service as partial and `misc.OMIT_FS` is `false`
- **WHEN** the operator runs `nodo download` for that service
- **THEN** the registry holds the complete service
- **AND** `.nofs` does not exist

### Requirement: Prune the filesystems of existing services
Setting `misc.OMIT_FS` to `true` SHALL NOT remove any stored data. `nodo storage:prune_blocks`
with `misc.OMIT_FS` `true` SHALL remove the filesystem of each complete service that has a
block, and write `.nofs` for it. A service with a live local instance SHALL stay complete,
because the node builds the instance again from the filesystem. With the option `false`, the
command SHALL NOT change the partial state of any service. A service that cannot be read
SHALL NOT make the command remove any block.

#### Scenario: Option set, no prune
- **GIVEN** the node holds complete services
- **WHEN** the operator sets `misc.OMIT_FS` to `true`
- **THEN** the services stay complete

#### Scenario: Prune with the option on
- **GIVEN** the node holds a complete service and `misc.OMIT_FS` is `true`
- **WHEN** the operator runs `nodo storage:prune_blocks`
- **THEN** the filesystem blocks of the service are removed
- **AND** the service has `.nofs`
- **AND** the specification of the service can still be read

#### Scenario: Shared block
- **GIVEN** a block is part of the filesystem of two services, and one of them stays complete
- **WHEN** the operator runs `nodo storage:prune_blocks` with `misc.OMIT_FS` `true`
- **THEN** the block stays on disk

#### Scenario: Live instance
- **GIVEN** a complete service has a live local instance
- **WHEN** the operator runs `nodo storage:prune_blocks` with `misc.OMIT_FS` `true`
- **THEN** the service stays complete and its blocks stay on disk

#### Scenario: Interrupted prune
- **WHEN** a block cannot be removed during the prune
- **THEN** the service already has `.nofs`
- **AND** a later prune finishes the removal

### Requirement: Integrity of a partial service
`nodo integrity` SHALL NOT report a partial service as damaged because its filesystem is not
there. The command SHALL show the service as partial. `--fix` SHALL NOT download the
filesystem of a partial service.

#### Scenario: Integrity check
- **GIVEN** the node holds a partial service
- **WHEN** the operator runs `nodo integrity`
- **THEN** the service is shown as partial and not as an error

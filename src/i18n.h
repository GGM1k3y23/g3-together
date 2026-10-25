// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once

namespace g3t::i18n {
enum Message
{
    NeedHealingPotion,
    HelpedUpWithPotion,
    HelpedUp,
    NobodyCanHelp,
    PartnerDownNeedsPotion,
    PartnerDown,
    PartnerDied,
    KnockedOutNeedsPotion,
    KnockedOut,
    RevivedByPartner,
    HelperHasNoPotion,
    JumpedToPartner,
    NoPartnerInWorld,
    CannotJumpWhileDown,
    JumpCooldown,
    NoCampaignHero,
    PartnerInOtherWorld,
    PartnerFarAway,
    FarFromHost,
    PartnerNearby,
    NearHost,
    HostInMenu,
    HostNewGame,
    HostLoadedSave,
    LoadingHostSave,
    PartnerOtherWorldSave,
    RecordRefusedWaiting,
    RecordBack,
    ConnectionLost,
    Reconnected,
    RefusedVersion,
    RefusedInstall,
    RefusedOther,
    SaveDamaged,
    PartnerWaitsForHero,
    OfferIncoming,
    OfferGold,
    OfferNothing,
    OfferDoneSeller,
    OfferDeclinedSeller,
    OfferExpiredSeller,
    OfferUnavailableSeller,
    OfferAcceptedBuyer,
    OfferDeclinedBuyer,
    OfferExpiredBuyer,
    OfferUnavailableBuyer,
    OfferBusyBuyer,
    KeyClash,
    HostUnreachable,
    HostReached,
    RobbedBy,
    Robbed,
    PlaceInUse,
    NpcTalking,
    MessageCount
};

// Local display only; language does not change the protocol or install parity.
// Formatted messages use the same printf placeholders in both languages.
wchar_t const* Text(Message message);
}

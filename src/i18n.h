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
    EpilogueWaits,
    SteamSearching,
    SteamConnecting,
    SteamUnavailableClient,
    SteamUnavailableHost,
    TradePriceHint,
    TradePricePay,
    TradePriceGet,
    TradePriceLimit,
    TradePriceNoGoods,
    MessageCount
};

// Local display only; language does not change the protocol or install parity.
// Formatted messages use the same printf placeholders in every language.
wchar_t const* Text(Message message);
// The language Text uses: en, de or pl. With language = auto it follows the game's text language (UserOptions.ini
// SubtitleLanguage, the options menu), asked again at every message
// (English while the game has none yet, and for languages the mod has no texts in).
char const* Language();
char const* LanguageFor(char const* game);  // a game text language (German, Polish, ...) -> de, pl, or en for any other
char const* GameTextLanguage();             // the game's current text language (hud.cpp: eCLocAdmin)
}

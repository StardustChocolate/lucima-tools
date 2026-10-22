"use strict";

(() => {
  const present = (value) => value !== undefined && value !== null;
  const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
  const stableKey = (value) => JSON.stringify(value, (_, item) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
  const eventConditionSubjects = { IsSameCamp: "allyMember", IsDiffCamp: "enemyMember" };
  const conditionEvents = { OtherDead: { who: "TriggerSource", label: "deathEvent" } };
  const eventConditionParts = (condition, trigger) => {
    const event = conditionEvents[trigger], subject = eventConditionSubjects[condition?.CheckFun];
    return event && subject && stableKey(condition) === stableKey({ Who: event.who, CheckFun: condition.CheckFun })
      ? { subject, event: event.label } : null;
  };
  const repeatsDisplayedCooldown = (condition, skill) => skill
    && Number.isFinite(skill.CoolDown_Final) && skill.CoolDown_Final > 0
    && stableKey(condition) === stableKey({ CheckFun: "CheckSkillCooldown", CheckID: skill.ID });
  // In this reflection path, the selected event target is the function user
  // and IsDamageSkill checks the current damage-hit event. Summarize only
  // that exact pair; keep every other condition, including the HP threshold.
  const reflectionRemainingConditions = (effect) => {
    if (effect?.Kind !== "Reflection" || effect.Function !== "AddHP"
        || effect.Value?.Property !== "EventHPDiff" || effect.Trigger !== "AfterEvent"
        || stableKey(effect.Target) !== stableKey({ Target: "SkillSource" })
        || !Array.isArray(effect.Conditions)) return null;
    const self = effect.Conditions.filter((condition) =>
      stableKey(condition) === stableKey({ Who: "SelectTarget", CheckFun: "IsSelf" }));
    const damage = effect.Conditions.filter((condition) =>
      stableKey(condition) === stableKey({ CheckFun: "IsDamageSkill" }));
    if (self.length !== 1 || damage.length !== 1) return null;
    return effect.Conditions.filter((condition) => condition !== self[0] && condition !== damage[0]);
  };
  // This audited single heal is already introduced by the skill's kill clause.
  // Omit only its repeated trigger text, retaining the raw effect and all
  // conditions that distinguish formulas, recipients, or soul variants.
  const describedHealCondition = (effect, skill) => {
    if (skill?.ID !== "H198S3"
        || !skill.Description_Markup?.includes("消灭敌人时，根据自身最大生命力恢復生命力")
        || !Number.isFinite(effect.Value?.Rate) || effect.Value.Rate <= 0
        || stableKey(effect) !== stableKey({
          Key: "H198S3/DynamicField1/0", Kind: "Heal", Function: "AddHP", Tags: "",
          Trigger: "", Target: { Target: "Self", TargetCamp: "My" },
          Value: { Rate: effect.Value.Rate, Property: "MaxHP" },
          Conditions: [{ Who: "SelectTarget", Property: "NowHP", Comparer: "Lt", Value: 0 }],
        })) return false;
    const modes = [skill.Scaling?.Normal, skill.Scaling?.Soul].filter(Boolean);
    return modes.length > 0 && modes.every((mode) => {
      const heals = (mode.Effects || []).filter((item) => ["Heal", "Revive"].includes(item.Kind));
      return !mode.References?.length && heals.length === 1 && stableKey(heals[0]) === stableKey(effect);
    });
  };
  const effectMeaning = (effect) => effect
    ? Object.fromEntries(Object.entries(effect).filter(([key]) => !["Key", "Target"].includes(key))) : null;
  // Trigger changes when the same effect is applied are presentation-only differences.
  // Keep Trigger in the raw effect, but omit it from the display merge signature.
  // This rule applies only to non-arithmetic repeated application effects;
  // arithmetic contribution rows remain explicitly excluded below.
  const effectDisplayMeaning = (effect) => effect
    ? Object.fromEntries(Object.entries(effect).filter(([key]) => !["Key", "Target", "Trigger"].includes(key))) : null;
  const selfExcludedHealMatches = (selected, self) => {
    if (selected?.Kind !== "Heal" || self?.Kind !== "Heal"
        || selected.Function !== "AddHP" || self.Function !== "AddHP") return false;
    const selfTarget = { Target: "Self", TargetCamp: "My" };
    const selectedTarget = { TargetCamp: "My" };
    if (stableKey(self.Target) !== stableKey(selfTarget)
        || ![selectedTarget, { ...selectedTarget, Target: "SkillSetting" }]
          .some((target) => stableKey(selected.Target) === stableKey(target))) return false;
    const guard = stableKey({ Who: "SelectTarget", CheckFun: "IsNoSelf" });
    const conditions = selected.Conditions || [];
    const remaining = conditions.filter((condition) => stableKey(condition) !== guard);
    if (conditions.length - remaining.length !== 1) return false;
    return stableKey(effectMeaning({ ...selected, Conditions: remaining })) === stableKey(effectMeaning(self));
  };
  const mergeSelfExcludedHeals = (effects, soulEffects) => {
    const consumed = new Set();
    return effects.flatMap((effect, index) => {
      if (consumed.has(index)) return [];
      for (let next = index + 1; next < effects.length; next++) {
        if (consumed.has(next)) continue;
        const candidate = effects[next];
        const [self, selected] = effect.Target?.Target === "Self" ? [effect, candidate] : [candidate, effect];
        if (!selfExcludedHealMatches(selected, self)) continue;
        const selfSoul = soulEffects.get(`${self.Key}/${self.Kind}`);
        const selectedSoul = soulEffects.get(`${selected.Key}/${selected.Kind}`);
        if ((selfSoul || selectedSoul) && !selfExcludedHealMatches(selectedSoul, selfSoul)) continue;
        consumed.add(next);
        // IsNoSelf partitions the recipients; the self row supplies the same
        // coefficient without incorrectly applying that guard to the whole heal.
        return [{ effect: self, members: [effect, candidate] }];
      }
      return [{ effect, members: [effect] }];
    });
  };
  const mergeTargetEffects = (effects, soulEffects = new Map()) => {
    const groups = new Map();
    return mergeSelfExcludedHeals(effects, soulEffects).filter(({ effect, members }) => {
      // Arithmetic contributions stay separate; only repeated application rows merge.
      if (["ConstAttack", "AttRate", "ConditionAttack", "CriticalDamageBonus", "PropertyRate"].includes(effect.Kind)) return true;
      const soul = soulEffects.get(`${effect.Key}/${effect.Kind}`);
      const signature = stableKey([effectDisplayMeaning(effect), effectDisplayMeaning(soul)]);
      const triggerPair = stableKey([effect?.Trigger ?? null, soul?.Trigger ?? null]);
      const targets = members.map((member) => stableKey(member.Target || {}));
      const soulTargets = members.map((member) => soulEffects.get(`${member.Key}/${member.Kind}`))
        .filter(Boolean).map((member) => stableKey(member.Target || {}));
      const matches = groups.get(signature) || [];
      // Preserve repeated doses to the same target when both applications use the
      // same trigger. Different triggers are separate execution paths but share
      // one displayed multiplier row when every other field matches.
      const match = matches.find((group) => {
        const disjointTargets = targets.every((target) => !group.targets.has(target))
          && soulTargets.every((target) => !group.soulTargets.has(target));
        return disjointTargets || !group.triggers.has(triggerPair);
      });
      if (match) {
        targets.forEach((target) => match.targets.add(target));
        soulTargets.forEach((target) => match.soulTargets.add(target));
        match.triggers.add(triggerPair);
        return false;
      }
      matches.push({ targets: new Set(targets), soulTargets: new Set(soulTargets), triggers: new Set([triggerPair]) });
      groups.set(signature, matches);
      return true;
    }).map(({ effect }) => effect);
  };
  const groupHealContributions = (effects, soulEffects) => {
    const context = (effect) => effect
      ? Object.fromEntries(Object.entries(effect).filter(([key]) => !["Key", "Value"].includes(key))) : null;
    const groups = [];
    for (const effect of effects) {
      const soul = soulEffects.get(`${effect.Key}/${effect.Kind}`);
      const eligible = effect.Kind === "Heal" && effect.Function === "AddHP" && effect.Value?.Property;
      const signature = eligible ? stableKey([context(effect), context(soul)]) : null;
      const previous = groups[groups.length - 1];
      // Share a heading for distinct property contributions to the same recipients.
      // Keep every formula, and check the application context in both modes.
      if (signature && previous?.signature === signature && previous.effects.every((item) =>
          item.Value.Property !== effect.Value.Property
          && (!soul || soulEffects.get(`${item.Key}/${item.Kind}`)?.Value?.Property !== soul.Value?.Property))) {
        previous.effects.push(effect);
      } else groups.push({ signature, effects: [effect] });
    }
    return groups.map((group) => group.effects);
  };
  const healActionType = (effect) => {
    if (effect?.Kind !== "Heal" || effect.Function !== "AddHP"
        || effect.Trigger !== "AnyEnemyAfterSkill"
        || stableKey(effect.Target) !== stableKey({ Target: "All", TargetCamp: "My" })
        || !Array.isArray(effect.Conditions) || effect.Conditions.length !== 2) return null;
    const action = effect.Conditions.find((condition) =>
      ["IsAddition", "IsCounter", "IsPinch"].includes(condition?.CheckFun));
    return action && stableKey(action) === stableKey({ CheckFun: action.CheckFun })
      && effect.Conditions.some((condition) => stableKey(condition) === stableKey({ CheckFun: "IsDamageSkill" }))
      ? action.CheckFun : null;
  };
  const healActionMeaning = (effect) => effect
    ? Object.fromEntries(Object.entries(effect).filter(([key]) => !["Key", "Conditions"].includes(key))) : null;
  const isActionHealGroup = (group, soulEffects) => {
    if (group.length !== 3 || !group.every(healActionType)
        || new Set(group.map(healActionType)).size !== 3
        || new Set(group.map((effect) => effect.Key)).size !== 3
        || !group.every((effect) => stableKey(healActionMeaning(effect)) === stableKey(healActionMeaning(group[0])))) return false;
    const souls = group.map((effect) => soulEffects.get(`${effect.Key}/${effect.Kind}`));
    return souls.every((soul) => !soul) || souls.every((soul, index) =>
      soul && healActionType(soul) === healActionType(group[index])
      && stableKey(healActionMeaning(soul)) === stableKey(healActionMeaning(souls[0])));
  };
  const groupActionHeals = (groups, soulEffects) => {
    const consumed = new Set();
    return groups.flatMap((group, index) => {
      if (consumed.has(index)) return [];
      if (group.length !== 1 || !healActionType(group[0])) return [group];
      const signature = stableKey(healActionMeaning(group[0]));
      const matching = groups.map((candidate, candidateIndex) => ({ candidate, candidateIndex }))
        .filter(({ candidate }) => candidate.length === 1 && healActionType(candidate[0])
          && stableKey(healActionMeaning(candidate[0])) === signature);
      const merged = matching.map(({ candidate }) => candidate[0]);
      // Native ActionType is a single enum: addition=3, counter=1, pinch=2.
      // Keep incomplete, duplicate or mismatched branches on the ordinary path.
      if (!isActionHealGroup(merged, soulEffects)) return [group];
      matching.forEach(({ candidateIndex }) => consumed.add(candidateIndex));
      return [merged];
    });
  };
  const directTargetCountFactor = (value) => value?.Property === "SkillDirectTargetsCount"
    && Object.keys(value).every((key) => ["Property", "Rate"].includes(key))
    && (Number.isFinite(value.Rate) || (value.Rate && Object.keys(value.Rate).length === 2
      && Number.isFinite(value.Rate.Min) && Number.isFinite(value.Rate.Max)));
  const campDebuffCount = (value) => String(value?.Property || "").trim() === "CampDebuffCount";
  const cappedCampDebuffRate = (value) => campDebuffCount(value)
    && Number.isFinite(value.Rate) && value.Rate >= 0
    && Number.isFinite(value.MaxValue) && value.MaxValue >= 0
    && Object.keys(value).every((key) => ["Who", "TargetInfo", "Property", "Rate", "MaxValue"].includes(key));
  const compactPenetration = (effects, skillID, soul) => {
    if (soul) return null;
    const terms = effects.filter((effect) => effect.Kind === "PenetrateRate");
    const differenceRule = {
      H187S3: { property: "MaxHP", direction: -1 },
      H206S3: { property: "Attack", direction: 1 },
    }[skillID];
    if (differenceRule) {
      if (terms.length !== 1) return null;
      const effect = terms[0], value = effect.Value;
      if (typeof effect.Key !== "string" || stableKey(effect) !== stableKey({
        Key: effect.Key, Kind: "PenetrateRate", Value: value,
        Conditions: [], Trigger: "", Target: {},
      })) return null;
      return Number.isFinite(value?.Rate) && value.Rate * differenceRule.direction > 0
        && Number.isFinite(value.FinalMaxValue) && value.FinalMaxValue >= 0
        && value.MustPositive === true
        && stableKey(value) === stableKey({ Property: differenceRule.property, Rate: value.Rate,
          FinalMaxValue: value.FinalMaxValue, MustPositive: true,
          ValueConvert: { Who: "Target", Property: differenceRule.property, Rate: -value.Rate } })
        ? { propertyDifference: effect } : null;
    }
    if (skillID !== "H176S3") return null;
    if (terms.length !== 2 || !terms.every((effect) =>
      typeof effect.Key === "string" && stableKey(effect) === stableKey({
        Key: effect.Key, Kind: "PenetrateRate", Value: effect.Value,
        Conditions: [], Trigger: "", Target: {},
      }))) return null;
    const base = terms.find((effect) => Number.isFinite(effect.Value?.Rate) && effect.Value.Rate >= 0
      && stableKey(effect.Value) === stableKey({ Rate: effect.Value.Rate, Property: "ValueOne" }));
    const difference = terms.find((effect) => effect !== base);
    const value = difference?.Value;
    return base && Number.isFinite(value?.Rate) && value.Rate > 0
      && Number.isFinite(value.FinalMaxValue) && value.FinalMaxValue >= 0
      && stableKey(value) === stableKey({ Property: "Speed", Rate: value.Rate,
        FinalMaxValue: value.FinalMaxValue, MustPositive: true,
        ValueConvert: { Who: "Target", Property: "Speed", Rate: -value.Rate } })
      ? { base, difference } : null;
  };
  const stackCounterDisplay = (item) => {
    const source = item?.StatusSource;
    if (!source || !Number.isInteger(source.MaxStack) || source.MaxStack <= 0
        || !Number.isFinite(item.Value?.Rate) || item.Value.Rate < 0
        || item.Trigger || stableKey(item.Target || {}) !== stableKey({})) return null;
    if (source.ID === "H012S2_3" && source.ProviderSkill === "H012S2"
        && item.Kind === "ConditionAttack" && !item.Conditions?.length
        && stableKey(item.Value) === stableKey({ Property: "BuffStack", PropertyID: source.ID, Rate: 1,
          ExtraRate: { Property: "BuffValue", PropertyID: source.ID, Rate: 1 } })) {
      const rate = source.StatusValue;
      const validRate = Number.isFinite(rate) ? rate >= 0 : rate
        && stableKey(rate) === stableKey({ Min: rate.Min, Max: rate.Max })
        && Number.isFinite(rate.Min) && rate.Min >= 0 && Number.isFinite(rate.Max) && rate.Max >= 0;
      const applications = source.Applications || [];
      const matches = (id, trigger, condition) => applications.filter((node) => {
        const { SourceKey, Trigger, ...fields } = node;
        return (Trigger || "AfterSkill") === trigger && stableKey(fields) === stableKey({
          ID: id, Fun: "AddStatus", TargetCamp: "My", Target: "Self", FID: source.ID, Value: rate, Condition: condition });
      }).length === 1;
      return validRate && applications.length === 2
        && matches("2_3", "AnyEnemyAfterSkill", { CheckFun: "IsNoDamageSkill" })
        && matches("2_6", "AfterSkill", { CheckFun: "SkillID", CheckID: "H012S3" })
        ? { label: "stackCount", percent: true } : null;
    }
    if (source.ID === "H208S3" && source.ProviderSkill === "H208S3"
        && item.Kind === "PenetrateRate"
        && stableKey(item.Value) === stableKey({ Rate: item.Value.Rate, Property: "BuffStack", PropertyID: source.ID })
        && stableKey(item.Conditions) === stableKey([{ CheckFun: "SkillID", CheckID: source.ID }])) {
      const applications = source.Applications || [];
      const matches = (trigger) => applications.filter((node) => {
        const { SourceKey, ...fields } = node;
        return Number.isFinite(node.Value) && node.Value >= 0 && stableKey(fields) === stableKey({
          Trigger: trigger, Fun: "AddTime", TargetCamp: "My", Target: "Self", Value: node.Value,
          Condition: { CheckFun: "IsUseSoul" }, SubFuns: [{ Fun: "AddStatus", TargetCamp: "My", Target: "Self", FID: source.ID }] });
      }).length === 1;
      return applications.length === 2 && matches("AnyEnemyAfterSkill") && matches("AnyAllyAfterSkill")
        ? { label: "soulStackCount", percent: true } : null;
    }
    const fixed = source.ID === "H145S3";
    const death = source.ID === "H166S3";
    if (!["H116S3", "H145S3", "H166S3", "H180S3"].includes(source.ID)
        || source.ApplyTrigger !== (death ? "OtherDead" : "AfterSkill")
        || (death && source.DeadCanRun !== false)
        || item.Kind !== (fixed ? "FixedDamage" : "ConditionAttack")
        || (fixed && (item.Function !== "DirectHPValueHit" || item.Tags !== "FixedDamage"))
        || stableKey(item.Value) !== stableKey({ Rate: item.Value.Rate,
          Property: fixed ? "BuffGroupStack" : "BuffStack", PropertyID: source.ID })
        || stableKey(item.Conditions) !== stableKey(fixed ? [{ CheckFun: "IsCriticalHit" }]
          : [{ CheckFun: "SkillID", CheckID: source.ID }])) return null;
    return { label: death ? "deathStackCount" : "castCount", percent: !fixed };
  };
  const castStackProperty = (item) => {
    const source = item?.StatusSource;
    if (source?.ID === "H147S2") {
      const value = item.Value?.Base;
      const valid = (number) => Number.isFinite(number) && number >= 0;
      const coefficient = valid(value) || (value && valid(value.Min) && valid(value.Max)
        && stableKey(value) === stableKey({ Min: value.Min, Max: value.Max }));
      return source.ProviderSkill === "H147S2" && source.ApplyTrigger === "AfterSkill"
        && source.IsStackMultiplyProp === null && source.EffectiveIsStackMultiplyProp === true
        && source.IsValueMultiplyProp === true && source.IsMergeProperty === true
        && source.DeadResident === null && source.PropertyValue === 1
        && stableKey(source.StatusValue) === stableKey(value)
        && Number.isInteger(source.MaxStack) && source.MaxStack > 0
        && item.Kind === "PropertyRate" && item.Property === "AttackFinalRate"
        && !item.Trigger && !item.Function && !item.Tags
        && stableKey(item.Target || {}) === stableKey({}) && stableKey(item.Conditions) === stableKey([])
        && coefficient && stableKey(item.Value) === stableKey({ Base: value });
    }
    return source?.ID === "H131S3" && source.ApplyTrigger === "AfterSkill"
      && source.IsStackMultiplyProp === true && source.IsMergeProperty === true
      && source.DeadResident === true && Number.isInteger(source.MaxStack) && source.MaxStack > 0
      && item.Kind === "PropertyRate" && item.Property === "AttackFinalRate"
      && !item.Trigger && !item.Function && !item.Tags
      && stableKey(item.Target || {}) === stableKey({}) && stableKey(item.Conditions) === stableKey([])
      && Number.isFinite(item.Value?.Base) && item.Value.Base >= 0
      && stableKey(item.Value) === stableKey({ Base: item.Value.Base });
  };
  const firstOwnSoulContribution = (item) => {
    const source = item?.StatusSource;
    if (item?.Kind !== "ConditionAttack" || source?.ID !== "H196S2" || source.ProviderSkill !== "H196S2"
        || source.MaxStack !== 1 || source.HitRemove !== true || source.Applications?.length !== 1
        || item.Trigger || stableKey(item.Target || {}) !== stableKey({}) || item.Conditions?.length
        || !Number.isFinite(item.Value?.Rate) || item.Value.Rate < 0
        || stableKey(item.Value) !== stableKey({ Rate: item.Value.Rate, Property: "ValueOne" })) return false;
    const { SourceKey, ...application } = source.Applications[0];
    return stableKey(application) === stableKey({ ID: "2_2", CD: 9999, Trigger: "BeforeSkill",
      Fun: "AddStatus", TargetCamp: "My", Target: "Self", FID: source.ID, Condition: { CheckFun: "IsUseSoul" } });
  };
  const groupCastFixedContributions = (groups, soulEffects, skillID) => {
    if (skillID !== "H145S3") return groups;
    const pairMatches = (base, stack) => {
      const context = (item) => Object.fromEntries(Object.entries(item)
        .filter(([key]) => !["Key", "Value", "StatusSource"].includes(key)));
      return base && stackCounterDisplay(stack) && stack.StatusSource.ID === skillID
        && !base.StatusSource && Number.isFinite(base.Value?.Base) && base.Value.Base >= 0
        && stableKey(base.Value) === stableKey({ Base: base.Value.Base })
        && stableKey(context(base)) === stableKey(context(stack));
    };
    const result = [];
    for (let index = 0; index < groups.length; index++) {
      const current = groups[index], next = groups[index + 1];
      if (current.length === 1 && next?.length === 1 && pairMatches(current[0], next[0])) {
        const baseSoul = soulEffects.get(`${current[0].Key}/${current[0].Kind}`);
        const stackSoul = soulEffects.get(`${next[0].Key}/${next[0].Kind}`);
        if ((!baseSoul && !stackSoul) || (pairMatches(baseSoul, stackSoul)
            && next[0].StatusSource.MaxStack === stackSoul.StatusSource.MaxStack)) {
          // Share the heading only. Both original fixed damage events remain separate rows.
          result.push([...current, ...next]);
          index++;
          continue;
        }
      }
      result.push(current);
    }
    return result;
  };
  const sharedHealBranches = (effect, soul) => {
    // Expand a shared conditional multiplier on multiple healing properties.
    // Keep unsupported expressions on the ordinary renderer instead of dropping fields.
    if (effect.Kind !== "Heal" || effect.Function !== "AddHP" || soul
        || effect.Conditions?.length || effect.Tags) return null;
    const terms = [];
    const rateValue = (value) => Number.isFinite(value)
      || (value && Object.keys(value).length === 2 && Number.isFinite(value.Min) && Number.isFinite(value.Max));
    for (let value = effect.Value; value; value = value.ValueConvert) {
      if (!Object.keys(value).every((key) => ["Who", "Property", "Rate", "ExtraRate", "ValueConvert"].includes(key))
          || !["Attack", "Defence", "MaxHP", "NowHP", "LastHP", "Speed"].includes(value.Property)
          || !rateValue(value.Rate)) return null;
      terms.push(value);
    }
    if (terms.length < 2) return null;
    const multiplier = terms[0].ExtraRate;
    if (!multiplier || !Object.keys(multiplier).every((key) => ["Base", "Property", "Rate", "Condition"].includes(key))
        || multiplier.Property !== "ValueOne" || !Number.isFinite(multiplier.Base)
        || !Number.isFinite(multiplier.Rate)
        || !terms.every((value) => stableKey(value.ExtraRate) === stableKey(multiplier))) return null;
    const condition = multiplier.Condition;
    const opposite = { Lt: "Gtn", Ltn: "Gt", Gt: "Ltn", Gtn: "Lt" };
    if (!condition || condition.Who !== "SelectTarget" || condition.Property !== "NowHPRate"
        || !opposite[condition.Comparer] || !Number.isFinite(condition.Value)
        || !Object.keys(condition).every((key) => ["Who", "Property", "Comparer", "Value"].includes(key))) return null;
    return [
      [{ ...condition, Comparer: opposite[condition.Comparer] }, multiplier.Base],
      [condition, multiplier.Base + multiplier.Rate],
    ].map(([branchCondition, factor]) => ({
      condition: branchCondition,
      values: terms.map(({ ExtraRate, ValueConvert, ...value }) => ({
        ...value,
        Rate: typeof value.Rate === "number" ? value.Rate * factor
          : { Min: value.Rate.Min * factor, Max: value.Rate.Max * factor },
      })),
    }));
  };
  const magnitude = (value) => typeof value === "object" && value ? Math.max(Math.abs(value.Min || 0), Math.abs(value.Max || 0)) : Math.abs(value || 0);
  const element = (tag, className = "", text = "") => {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  };

  class ScalingView {
    constructor(data, translations) {
      this.data = data;
      this.map = translations.scaling_CHS || {};
      this.labels = this.map.labels || {};
      this.specialResourceName = translations.values_CHS?.SpecialResource?.[data.SPType] || "";
      this.elementNames = translations.values_CHS?.Element || {};
      this.skills = new Map((data.Skill || []).map((skill) => [skill.ID, skill]));
      this.names = new Map();
      for (const skill of data.Skill || []) {
        this.names.set(skill.ID, skill.NAME || skill.ID);
        for (const status of Array.isArray(skill.Status) ? skill.Status : []) {
          this.names.set(status.ID, status.Name || status.ID);
        }
      }
    }

    label(key) {
      return this.labels[key] || key;
    }

    number(value, percent = false, bracketRange = false, signed = false) {
      if (value && typeof value === "object" && "Min" in value) {
        const start = this.number(value.Min, percent, false, signed);
        const end = this.number(value.Max, percent, false, signed);
        return bracketRange ? `[${start}-${end}]` : `${start}～${end}`;
      }
      if (!present(value)) return "—";
      return `${signed && Number(value) > 0 ? "+" : ""}${Number((Number(value) * (percent ? 100 : 1)).toFixed(6))}${percent ? "%" : ""}`;
    }

    pair(value, soulValue, percent = false, bracketRange = false, signed = false) {
      const fragment = document.createDocumentFragment();
      fragment.append(document.createTextNode(this.number(value, percent, bracketRange, signed)));
      if (present(soulValue) && !same(value, soulValue)) {
        fragment.append(element("span", "scaling-soul", `(${this.number(soulValue, percent, bracketRange, signed)})`));
      }
      return fragment;
    }

    who(code) {
      if (code !== undefined && code !== null && Object.prototype.hasOwnProperty.call(this.map.who || {}, code)) {
        return this.map.who[code];
      }
      return code || "";
    }

    property(value) {
      const info = value.TargetInfo;
      let owner = value.__displaySelf ? "自身" : this.who(value.Who);
      if (info?.TargetType === "MaxProperty" && info.Property === "Attack" && info.TargetCamp === "My") {
        owner = this.label("highestAttackAlly");
      }
      // NowSP belongs to the value's owner. Only the current character's
      // resource type is known here; other targets keep the generic label.
      const ownResource = value.Property === "NowSP" && (!value.Who || value.Who === "Source")
        && !value.TargetInfo && this.specialResourceName;
      let property = ownResource || this.map.properties?.[value.Property] || value.Property || "";
      if (campDebuffCount(value)) {
        // TargetInfo selects the reference role before Who is considered.
        // Only the audited All selector identifies a camp here; other selectors
        // may follow event targets regardless of their TargetCamp setting.
        const camp = info
          ? (info.TargetType === "All" ? (info.TargetCamp === undefined ? "Enemy" : info.TargetCamp) : null)
          : (!value.Who || value.Who === "Source" ? "My" : null);
        property = camp === "Enemy" ? this.label("enemyDebuffTotal")
          : camp === "My" ? this.label("allyDebuffTotal") : this.map.properties?.CampDebuffCount || "CampDebuffCount";
        if (info || camp === "My" || camp === "Enemy") owner = "";
      }
      const countProperty = ["DebuffCount", "BuffCount", "BuffCountByID", "BuffGroupStack", "BuffStack", "BuffCountByGroup"].includes(value.Property);
      const identifier = value.PropertyID
        ? `${countProperty ? " · " : "·"}${this.names.get(value.PropertyID) || value.PropertyID}`
        : "";
      if (value.Property === "OldBuffCount") return `解除前${owner}${property}${identifier}`;
      return `${owner}${property}${identifier}`;
    }

    samePropertySources(value, soul) {
      const source = (entry) => [entry?.Who || "", entry?.Property || "", entry?.PropertyID || "", entry?.TargetInfo || null];
      if (!same(source(value), source(soul))) return false;
      for (const field of ["ValueConvert", "ExtraRate", "FinalMaxValueConvert"]) {
        if ((value?.[field] || soul?.[field]) && !this.samePropertySources(value?.[field], soul?.[field])) return false;
      }
      const branches = value?.ConditionRates || [];
      const soulBranches = soul?.ConditionRates || [];
      return branches.length === soulBranches.length
        && branches.every((branch, index) => this.samePropertySources(branch, soulBranches[index]));
    }

    conditions(values, trigger = "", displayedSkill = null) {
      return (values || []).filter((condition) => !repeatsDisplayedCooldown(condition, displayedSkill)).map((condition) => {
        let text = "";
        if (condition.Property) {
          const comparator = this.map.comparers?.[condition.Comparer] || condition.Comparer || "=";
          const percent = condition.Property === "NowHPRate";
          const value = condition.ValueConvert ? JSON.stringify(condition.ValueConvert) : this.number(condition.Value, percent);
          text = `${this.property(condition)} ${comparator} ${value}`;
          if (condition.CheckID) text += ` · ${this.names.get(condition.CheckID) || condition.CheckID}`;
        } else if (condition.CheckFun) {
          const underAttack = trigger === "TargetAfterSkill"
            && stableKey(condition) === stableKey({ CheckFun: "IsDamageSkill" });
          const event = eventConditionParts(condition, trigger);
          text = event ? `${this.label(event.subject)}${this.label(event.event)}`
            : underAttack ? this.label("underAttack") : (this.map.checks?.[condition.CheckFun] || condition.CheckFun);
          if (condition.CheckID) {
            const name = condition.CheckFun === "RoleElement" ? this.elementNames[condition.CheckID]
              : condition.CheckFun === "CheckSkillCooldown" ? this.skills.get(condition.CheckID)?.NAME
                : this.names.get(condition.CheckID);
            text += ` · ${name || condition.CheckID}`;
          }
          if (condition.Who && !event) text = `${this.who(condition.Who)} · ${text}`;
        }
        const nested = this.conditions([
          ...(condition.Condition ? [condition.Condition] : []), ...(condition.Conditions || [])
        ]);
        if (nested) text = [text, nested].filter(Boolean).join(` ${this.label("and")} `);
        if (condition.Invert) text = `${this.label("not")}（${text}）`;
        return text || JSON.stringify(condition);
      }).join(` ${this.label("and")} `);
    }

    compactCondition(values) {
      if (!Array.isArray(values) || values.length !== 1) return "";
      const condition = values[0];
      if (!condition || condition.CheckFun !== "HaveBuff" || !condition.CheckID
          || condition.Condition || condition.Conditions?.length) return "";
      return this.names.get(condition.CheckID) || condition.CheckID;
    }

    appendTerm(container, term, addition = false, multiline = false, showOperator = true) {
      if (addition && multiline) {
        const row = element("span", "scaling-addition");
        if (showOperator) row.append(element("span", "scaling-operator", "+ "));
        row.append(term);
        container.append(row);
      } else {
        if (addition) container.append(document.createTextNode(" + "));
        container.append(term);
      }
    }

    expression(value = {}, soul, kind = "ConstAttack", showConditions = true, multiline = false, bracketRange = ["Heal", "Shield", "Reflection", "PropertyRate"].includes(kind), signed = kind === "CriticalDamageBonus") {
      const fragment = document.createDocumentFragment();
      if (soul && !this.samePropertySources(value, soul)) {
        fragment.append(this.expression(value, undefined, kind, showConditions, multiline, bracketRange, signed));
        const variant = element("span", "scaling-soul scaling-soul-expression", "(");
        variant.append(this.expression(soul, undefined, kind, showConditions, multiline, bracketRange, signed), document.createTextNode(")"));
        fragment.append(variant);
        return fragment;
      }
      const conditional = ["ConditionAttack", "AttRate", "CriticalDamageBonus", "PropertyRate", "PenetrateRate"].includes(kind);
      const showAdditionOperator = !["ConstAttack", "AttRate", "Heal", "Shield"].includes(kind);
      if (Array.isArray(value.ConditionRates) && value.ConditionRates.length) {
        value.ConditionRates.forEach((branch, index) => {
          if (index) fragment.append(document.createTextNode("；"));
          const branchValue = { ...value, ...branch };
          delete branchValue.ConditionRates;
          delete branchValue.Condition;
          const branchSoul = soul?.ConditionRates?.[index] ? { ...soul, ...soul.ConditionRates[index] } : undefined;
          if (branchSoul) delete branchSoul.ConditionRates;
          if (showConditions) fragment.append(document.createTextNode(`${this.conditions(branch.Condition ? [branch.Condition] : branch.Conditions)}：`));
          fragment.append(this.expression(branchValue, branchSoul, kind, showConditions, false, bracketRange, signed));
        });
        return fragment;
      }
      const child = value.ValueConvert;
      const speedDifference = value.Property === "Speed" && child?.Property === "Speed"
        && typeof value.Rate === "number" && child.Rate === -value.Rate;
      const negativeBaseOffset = kind === "ConditionAttack"
        && value.Property && typeof value.Rate === "number" && typeof value.Base === "number"
        && value.Base < 0 && !child && !value.Condition && !(value.Conditions || []).length
        && !value.ExtraRate && !magnitude(value.ExtraRatePlus);
      if (negativeBaseOffset) {
        fragment.append(document.createTextNode("("));
        fragment.append(this.pair(value.Rate, soul?.Rate, true, bracketRange));
        fragment.append(document.createTextNode(` × ${this.property(value)}) - `));
        const soulBase = typeof soul?.Base === "number" ? Math.abs(soul.Base) : soul?.Base;
        fragment.append(this.pair(Math.abs(value.Base), soulBase, true, bracketRange));
        return fragment;
      }
      // Fixed damage values such as H138S3/H142S3 are written as a fixed
      // base plus a resource/count contribution. Keep the readable order
      // “base + contribution”; ordinary expressions retain their existing
      // ordering and operator rules.
      const fixedDamageBaseFirst = kind === "FixedDamage"
        && magnitude(value.Base)
        && value.Property && present(value.Rate)
        && !child && !value.Condition && !(value.Conditions || []).length
        && !value.ExtraRate && !magnitude(value.ExtraRatePlus);
      let hasTerm = false;
      if (fixedDamageBaseFirst) {
        this.appendTerm(fragment, this.pair(value.Base, soul?.Base, conditional, bracketRange, signed), false,
          multiline, showAdditionOperator);
        hasTerm = true;
      }
      if (speedDifference) {
        fragment.append(this.pair(value.Rate, soul?.Rate, true, bracketRange, signed));
        fragment.append(document.createTextNode(` ×（${this.property(value)} − ${this.property(child)}）`));
        hasTerm = true;
      } else if (value.Property && present(value.Rate)) {
        const percentRate = conditional || (kind === "Reflection" && value.Property === "EventHPDiff")
          || ["Attack", "Defence", "HP", "MaxHP", "NowHP", "LastHP", "Speed", "SkillDamage", "RealSkillDamage", "AllDamage", "InjuryValue"].includes(value.Property);
        if (fixedDamageBaseFirst) fragment.append(document.createTextNode(" + "));
        fragment.append(this.pair(value.Rate, soul?.Rate, percentRate, bracketRange, signed));
        if (value.Property !== "ValueOne") {
          // Count based condition terms are multiplied by the displayed rate:
          // e.g. "20% × 目标减益数量". Keep the ordinary percentage wording
          // for direct stat properties such as Attack and Speed.
          const countProperty = campDebuffCount(value) || ["DebuffCount", "BuffCount", "BuffCountByID", "BuffGroupStack", "BuffStack", "BuffCountByGroup", "NowSP"].includes(value.Property);
          const separator = kind === "PropertyRate" && value.Property !== "NowSP" && !campDebuffCount(value)
            ? " " : (countProperty ? " × " : (percentRate ? " " : " × "));
          // Plain AllDamage healing reads the current skill event, not the
          // role selected by Who. Keep that source field intact in the data.
          const eventDamageHeal = kind === "Heal" && value.Property === "AllDamage"
            && value.Who === "SelectTarget"
            && Object.keys(value).every((key) => ["Who", "Property", "Rate"].includes(key));
          const propertyValue = eventDamageHeal ? { Property: value.Property } : value;
          fragment.append(document.createTextNode(`${separator}${this.property(propertyValue)}`));
          if (present(value.MaxValue)) {
            const rateCap = kind === "PropertyRate" && cappedCampDebuffRate(value)
              && (!soul || cappedCampDebuffRate(soul));
            if (rateCap) {
              // MaxValue caps the status count before multiplication by Rate.
              fragment.append(document.createTextNode(`（${this.label("limit")} `));
              fragment.append(this.pair(value.Rate * value.MaxValue,
                soul ? soul.Rate * soul.MaxValue : undefined, true, bracketRange));
              fragment.append(document.createTextNode("）"));
            } else fragment.append(document.createTextNode(`（${this.label("limit")} ${this.number(value.MaxValue, false, bracketRange)}）`));
          }
        }
        hasTerm = true;
      }
      const conditionValues = [...(value.Condition ? [value.Condition] : []), ...(value.Conditions || [])];
      const conditions = this.conditions(conditionValues);
      if (showConditions && conditions && hasTerm) {
        const compact = this.compactCondition(conditionValues);
        fragment.append(element("span", "scaling-condition", compact ? `（${compact}）` : `（${conditions}）`));
      }
      if (value.ExtraRate || magnitude(value.ExtraRatePlus)) {
        const targetCount = directTargetCountFactor(value.ExtraRate)
          && (!soul || directTargetCountFactor(soul.ExtraRate))
          && !present(value.ExtraRatePlus) && !present(soul?.ExtraRatePlus);
        if (targetCount) {
          fragment.append(document.createTextNode(" × "));
          if (value.ExtraRate.Rate !== 1 || (soul && soul.ExtraRate.Rate !== 1)) {
            fragment.append(this.pair(value.ExtraRate.Rate, soul?.ExtraRate?.Rate, false, bracketRange));
            fragment.append(document.createTextNode(" × "));
          }
          fragment.append(document.createTextNode(this.property(value.ExtraRate)));
        } else {
          fragment.append(document.createTextNode(" ×（"));
          if (value.ExtraRate) fragment.append(this.expression(value.ExtraRate, soul?.ExtraRate, "ConditionAttack", showConditions, false, bracketRange));
          else fragment.append(document.createTextNode("1"));
          if (magnitude(value.ExtraRatePlus)) {
            fragment.append(document.createTextNode(" + "));
            fragment.append(this.pair(value.ExtraRatePlus, soul?.ExtraRatePlus, false, bracketRange));
          }
          fragment.append(document.createTextNode("）"));
        }
      }
      if (!fixedDamageBaseFirst && present(value.Base) && (magnitude(value.Base) || !hasTerm)) {
        this.appendTerm(fragment, this.pair(value.Base, soul?.Base, conditional, bracketRange, signed && !hasTerm), hasTerm, multiline, showAdditionOperator);
        hasTerm = true;
      }
      if (child && !speedDifference) {
        this.appendTerm(fragment, this.expression(child, soul?.ValueConvert, kind, showConditions, multiline, bracketRange, signed && !hasTerm), hasTerm, multiline, showAdditionOperator);
        hasTerm = true;
      }
      if (!hasTerm) fragment.append(document.createTextNode(this.label("unknown")));
      if (value.MustPositive) {
        const floor = speedDifference && kind === "ConditionAttack"
          ? `（${this.label("floor")} ${this.number(0, true)}）`
          : `；${this.label("positive")}`;
        fragment.append(document.createTextNode(floor));
      }
      if (present(value.FinalMaxValue)) {
        fragment.append(document.createTextNode(`（${this.label("limit")} `));
        fragment.append(this.pair(value.FinalMaxValue, soul?.FinalMaxValue, conditional, bracketRange));
        fragment.append(document.createTextNode("）"));
      }
      if (value.FinalMaxValueConvert) {
        fragment.append(document.createTextNode(`（${this.label("limit")} `));
        fragment.append(this.expression(value.FinalMaxValueConvert, soul?.FinalMaxValueConvert, kind, showConditions, false, bracketRange, false));
        fragment.append(document.createTextNode("）"));
      }
      if (present(value.FinalMinValue)) {
        fragment.append(document.createTextNode(`；${this.label("floor")} `));
        fragment.append(this.pair(value.FinalMinValue, soul?.FinalMinValue, conditional, bracketRange));
      }
      return fragment;
    }

    group(container, label, className = "") {
      const group = element("div", `scaling-group ${className}`.trim());
      group.append(element("h4", "scaling-label", `${label}：`));
      const body = element("div", "scaling-value");
      group.append(body);
      container.append(group);
      return body;
    }

    effect(container, effect, burst, showConditions, displayedSkill = null) {
      const line = element("div", "scaling-term");
      const counter = stackCounterDisplay(effect);
      if (counter && (!burst || (same(stackCounterDisplay(burst), counter)
          && burst.StatusSource.ID === effect.StatusSource.ID
          && burst.StatusSource.MaxStack === effect.StatusSource.MaxStack))) {
        const fromStatusValue = effect.StatusSource.ID === "H012S2_3";
        line.append(this.pair(fromStatusValue ? effect.StatusSource.StatusValue : effect.Value.Rate,
          fromStatusValue ? burst?.StatusSource.StatusValue : burst?.Value.Rate, counter.percent, fromStatusValue));
        const cap = this.label("castCountCap").replace("{count}", this.number(effect.StatusSource.MaxStack));
        line.append(document.createTextNode(` × ${this.label(counter.label)}（${cap}）`));
        container.append(line);
        return line;
      }
      if (firstOwnSoulContribution(effect) && (!burst || firstOwnSoulContribution(burst))) {
        line.append(this.pair(effect.Value.Rate, burst?.Value.Rate, true));
        line.append(element("span", "scaling-condition", `（${this.label("firstOwnSoul")}）`));
        container.append(line);
        return line;
      }
      const propertyRateName = {
        AttackValue: "攻击力", AttackFinalRate: "攻击力", DefenceFinalRate: "防御力", SpeedFinalRate: "速度",
        CriticalRate: "暴击率", DeCriticalRate: "暴击抵抗率",
      }[effect.Property] || effect.Property || "属性";
      if (effect.Kind === "PropertyRate") {
        line.append(element("h5", "scaling-property-label", `${propertyRateName}：`));
        if (castStackProperty(effect) && (!burst || (castStackProperty(burst)
            && burst.StatusSource.MaxStack === effect.StatusSource.MaxStack))) {
          line.append(this.pair(effect.Value.Base, burst?.Value.Base, true, true));
          const cap = this.label("castCountCap").replace("{count}", this.number(effect.StatusSource.MaxStack));
          const capText = element("span", "", `（${cap}）`);
          capText.style.whiteSpace = "nowrap";
          const counterLabel = effect.StatusSource.ID === "H147S2" ? "stackCount" : "castCount";
          line.append(document.createTextNode(` × ${this.label(counterLabel)}`), capText);
        } else {
          line.append(this.expression(effect.Value, burst?.Value, effect.Kind, showConditions, false, true));
        }
        container.append(line);
        return line;
      }
      const cappedBuffAttack = (value) => value?.Property === "BuffCount" && value.Who === "Source"
        && Number.isFinite(value.Rate) && value.Rate >= 0
        && Number.isFinite(value.MaxValue) && value.MaxValue >= 0
        && Object.keys(value).every((key) => ["Who", "Property", "Rate", "MaxValue"].includes(key));
      if (effect.Kind === "AttRate" && cappedBuffAttack(effect.Value) && (!burst || cappedBuffAttack(burst.Value))) {
        // MaxValue caps the count before Rate; display the resulting attack coefficient cap.
        line.append(this.pair(effect.Value.Rate, burst?.Value.Rate, true));
        line.append(document.createTextNode(` ${this.property({ Property: "Attack" })} × ${this.property({ Property: "BuffCount" })}(${this.label("limit")}`));
        line.append(this.pair(effect.Value.Rate * effect.Value.MaxValue,
          burst ? burst.Value.Rate * burst.Value.MaxValue : undefined, true));
        line.append(document.createTextNode(")"));
      } else {
        const valueConditions = [...(effect.Value?.Condition ? [effect.Value.Condition] : []), ...(effect.Value?.Conditions || [])];
        const postfixedValueCondition = effect.Kind === "AttRate"
          && effect.Value?.Property === "ValueOne" && valueConditions.length;
        line.append(this.expression(effect.Value, burst?.Value, effect.Kind,
          showConditions && !postfixedValueCondition, effect.Kind !== "AttRate"));
        if (effect.Kind === "AttRate") {
          line.append(document.createTextNode(` ${this.property({ Property: "Attack" })}`));
          if (showConditions && postfixedValueCondition) {
            const compact = this.compactCondition(valueConditions);
            const conditions = this.conditions(valueConditions);
            line.append(element("span", "scaling-condition", compact ? `（${compact}）` : `（${conditions}）`));
          }
        }
      }
      const redundantCountCondition = effect.Kind === "ConditionAttack"
        && effect.Conditions?.length === 1
        && effect.Value?.Property === "BuffCountByID"
        && effect.Value?.PropertyID
        && effect.Value?.Rate >= 0
        && effect.Conditions[0]?.Property === effect.Value.Property
        && effect.Conditions[0]?.CheckID === effect.Value.PropertyID
        && effect.Conditions[0]?.Who === effect.Value.Who
        && effect.Conditions[0]?.Comparer === "Gt"
        && Number(effect.Conditions[0]?.Value) === 1;
      if (showConditions && effect.Conditions?.length && !redundantCountCondition
          && !describedHealCondition(effect, displayedSkill)) {
        const remaining = reflectionRemainingConditions(effect);
        const reflection = remaining !== null
          && (!burst || stableKey(reflectionRemainingConditions(burst)) === stableKey(remaining));
        const compact = reflection ? "" : this.compactCondition(effect.Conditions);
        const detail = this.conditions(reflection ? remaining : effect.Conditions, effect.Trigger, displayedSkill);
        const conditions = reflection
          ? [this.label("underAttack"), detail].filter(Boolean).join(` ${this.label("and")} `) : detail;
        if (compact || conditions) line.append(element("span", "scaling-condition", compact ? `（${compact}）` : `（${conditions}）`));
      }
      container.append(line);
      return line;
    }

    linkedSoul(reference, linked, source) {
      // Alternate forms are selectable; a replacement keeps the original
      // action's soul flag. A passive can declare a replacement for another skill.
      if (reference.Kind === "TwinSkill") return linked?.Scaling?.Soul;
      if (reference.Kind !== "ReplaceSkill") return null;
      const sourceIds = new Set();
      const collect = (condition) => {
        if (!condition || condition.Invert) return;
        if (condition.CheckFun === "SkillID" && condition.CheckID) sourceIds.add(condition.CheckID);
        collect(condition.Condition);
        (condition.Conditions || []).forEach(collect);
      };
      (reference.Conditions || []).forEach(collect);
      const originals = sourceIds.size ? [...sourceIds].map((id) => this.skills.get(id)) : [source];
      return originals.some((skill) => Number(skill?.SoulBurst?.SkillSoulCost) > 0) ? linked?.Scaling?.Soul : null;
    }

    hasValues(skill, soul = null, visited = new Set()) {
      const mode = skill.Scaling?.Normal;
      if (!mode && !soul) return false;
      if ([mode, soul].some((value) => magnitude(value?.Attack) || value?.Effects?.length)) return true;
      return [...(mode?.References || []), ...(soul?.References || [])].some((reference) => {
        if (visited.has(reference.SkillID)) return false;
        const linked = this.skills.get(reference.SkillID);
        return !linked || linked.missing || this.hasValues(linked,
          this.linkedSoul(reference, linked, skill), new Set([...visited, reference.SkillID]));
      });
    }

    mode(container, skill, normal, soul, visited, showConditions = true) {
      // Only the root skill has its cooldown displayed in the skill header.
      const displayedSkill = visited.size === 1 ? skill : null;
      const effects = normal.Effects || [];
      const soulEffects = new Map((soul?.Effects || []).map((effect) => [`${effect.Key}/${effect.Kind}`, effect]));
      const normalKeys = new Set(effects.map((effect) => `${effect.Key}/${effect.Kind}`));
      const soulOnlyEffects = (soul?.Effects || []).filter((effect) => !normalKeys.has(`${effect.Key}/${effect.Kind}`));
      const plainTerm = (effect) => effect.Kind === "ConstAttack" && !effect.Conditions?.length
        && !effect.Value.Condition && !effect.Value.Conditions?.length && !effect.Value.ConditionRates;
      const basicEffects = effects.filter((effect) => ["ConstAttack", "AttRate"].includes(effect.Kind));
      // A passive upgrade that explicitly targets its ReplaceSkill is an
      // upgrade of that replacement form, rather than a condition multiplier
      // belonging to the passive itself. The effective replacement retains
      // the source effect and renders its level endpoint below.
      const isReplacementUpgrade = (effect) => {
        if (effect.Kind !== "ConditionAttack" || effect.Value?.Property !== "ValueOne") return false;
        const condition = effect.Value?.Condition;
        if (condition?.CheckFun !== "SkillID" || !condition.CheckID) return false;
        const sourceID = String(effect.Key || "").split("/")[0];
        const source = this.skills.get(sourceID);
        return (source?.Scaling?.Normal?.References || [])
          .some((reference) => reference.Kind === "ReplaceSkill" && reference.SkillID === condition.CheckID);
      };
      const replacementUpgradeEffects = effects.filter(isReplacementUpgrade);
      // Counter-skill upgrades may carry the SkillID guard on the function,
      // rather than inside Value. Only reclassify a pure level-dependent
      // constant supplied by the passive that references this counter skill.
      const isCounterUpgrade = (effect) => {
        if (effect.Kind !== "ConditionAttack" || effect.Trigger
            || stableKey(effect.Target || {}) !== stableKey({})) return false;
        const value = effect.Value;
        if (!Number.isFinite(value?.Rate?.Max) || value.Rate.Max <= 0
            || stableKey(value) !== stableKey({ Property: "ValueOne", Rate: { Min: 0, Max: value.Rate.Max } })
            || stableKey(effect.Conditions) !== stableKey([{ CheckFun: "SkillID", CheckID: skill.ID }])) return false;
        const sourceID = String(effect.Key || "").split("/")[0];
        const source = this.skills.get(sourceID);
        return sourceID !== skill.ID && source?.SkillType === "Passive"
          && (source.Scaling?.Normal?.References || []).some((reference) =>
            reference.Kind === "CounterSkill" && reference.SkillID === skill.ID);
      };
      const counterUpgradeEffects = effects.filter((effect) => {
        const soulEffect = soulEffects.get(`${effect.Key}/${effect.Kind}`);
        return isCounterUpgrade(effect) && (!soulEffect || isCounterUpgrade(soulEffect));
      });
      // Addition-skill upgrades keep the SkillID guard either inside Value or
      // on the function. Inner guards retain the same effect on both skills;
      // outer guards are injected into the linked attack only. Both forms must
      // be pure level ranges from the referencing passive, without soul variants.
      const additionUpgradeTarget = (effect) => {
        if (effect.Kind !== "ConditionAttack" || effect.Trigger
            || Object.keys(effect).some((key) =>
              !["Key", "Kind", "Value", "Conditions", "Trigger", "Target"].includes(key))
            || stableKey(effect.Target || {}) !== stableKey({})) return null;
        const value = effect.Value;
        const outerGuard = effect.Conditions?.length > 0;
        const conditionField = Object.hasOwn(value || {}, "Condition") ? "Condition" : "Conditions";
        const targetID = outerGuard ? effect.Conditions[0]?.CheckID
          : conditionField === "Condition" ? value.Condition?.CheckID : value?.Conditions?.[0]?.CheckID;
        const condition = { CheckFun: "SkillID", CheckID: targetID };
        const expectedValue = { Property: "ValueOne", Rate: { Min: 0, Max: value?.Rate?.Max } };
        if (!outerGuard) expectedValue[conditionField] = conditionField === "Condition" ? condition : [condition];
        if (!targetID || !Number.isFinite(value?.Rate?.Max) || value.Rate.Max <= 0
            || stableKey(value) !== stableKey(expectedValue)
            || stableKey(effect.Conditions) !== stableKey(outerGuard ? [condition] : [])
            || (outerGuard && (skill.ID !== targetID || stableKey(effect.Target) !== stableKey({})))) return null;
        const sourceID = String(effect.Key || "").split("/")[0];
        const source = this.skills.get(sourceID);
        const linked = this.skills.get(targetID);
        if (sourceID === targetID || ![sourceID, targetID].includes(skill.ID)
            || source?.SkillType !== "Passive" || !linked || linked.missing
            || source.Scaling?.Soul || linked.Scaling?.Soul
            || (skill.ID === sourceID && visited.has(targetID))) return null;
        if (!(source.Scaling?.Normal?.References || []).some((reference) =>
          reference.Kind === "AdditionSkill" && reference.SkillID === targetID)) return null;
        if (outerGuard) {
          const hasCycle = (item, path) => (item.Scaling?.Normal?.References || []).some((reference) => {
            if (path.has(reference.SkillID)) return true;
            const next = this.skills.get(reference.SkillID);
            return next && hasCycle(next, new Set([...path, reference.SkillID]));
          });
          if (hasCycle(linked, new Set([...visited, sourceID, targetID]))) return null;
        }
        return (outerGuard ? [linked] : [source, linked]).every((item) => {
          const matches = (item.Scaling?.Normal?.Effects || []).filter((candidate) =>
            candidate.Key === effect.Key && candidate.Kind === effect.Kind);
          return matches.length === 1 && stableKey(matches[0]) === stableKey(effect);
        }) ? targetID : null;
      };
      const additionUpgradeEffects = effects.filter((effect) => additionUpgradeTarget(effect));
      const extraEffects = effects.filter((effect) => effect.Kind === "ConditionAttack"
        && !isReplacementUpgrade(effect) && !counterUpgradeEffects.includes(effect)
        && !additionUpgradeEffects.includes(effect));
      const hasAttack = present(normal.Attack) && (magnitude(normal.Attack) || magnitude(soul?.Attack));
      if (hasAttack || basicEffects.length) {
        const body = this.group(container, this.label("base"));
        // Some skills encode a conditional max-HP contribution and a separate
        // ValueOne attack contribution that share the same branch condition.
        // Render those branches together so the displayed attack coefficient
        // reflects their combined result while leaving the parsed Effects
        // untouched. Restrict this shape to the two-effect pattern to avoid
        // changing ordinary condition-rate rows elsewhere in the archive.
        const conditionalConst = skill.ID === "H042S3" && basicEffects.length === 2
          ? basicEffects.find((effect) => effect.Kind === "ConstAttack"
            && !effect.Conditions?.length
            && Array.isArray(effect.Value?.ConditionRates)
            && effect.Value.ConditionRates.length)
          : null;
        const conditionalAttack = skill.ID === "H042S3" && basicEffects.length === 2
          ? basicEffects.find((effect) => effect.Kind === "AttRate"
            && !effect.Conditions?.length
            && effect.Value?.Property === "ValueOne"
            && (effect.Value.Condition || effect.Value.Conditions?.length))
          : null;
        const conditionKey = (value) => stableKey(value?.Condition
          ? [value.Condition] : (value?.Conditions || []));
        const conditionalBranches = conditionalConst && conditionalAttack
          && conditionalConst.Value.ConditionRates.some((branch) =>
            conditionKey(branch) === conditionKey(conditionalAttack.Value))
          ? conditionalConst.Value.ConditionRates
          : null;
        if (conditionalBranches) body.parentElement.classList.add("scaling-group--branching");
        if (conditionalBranches) {
          const addValues = (left, right) => {
            if (typeof left === "number" && typeof right === "number") return left + right;
            if (left && typeof left === "object" && right && typeof right === "object"
                && "Min" in left && "Max" in left && "Min" in right && "Max" in right) {
              return { Min: left.Min + right.Min, Max: left.Max + right.Max };
            }
            return left;
          };
          const constSoul = soulEffects.get(`${conditionalConst.Key}/${conditionalConst.Kind}`)?.Value;
          const attackSoul = soulEffects.get(`${conditionalAttack.Key}/${conditionalAttack.Kind}`)?.Value;
          conditionalBranches.forEach((branch, index) => {
            // The native condition omits Who because it defaults to Source;
            // make that scope explicit in this grouped display only.
            const branchCondition = (branch.Condition ? [branch.Condition] : (branch.Conditions || []))
              .map((condition) => condition.Who
                ? { ...condition, __displaySelf: true }
                : { ...condition, Who: "Source", __displaySelf: true });
            const header = element("div", "scaling-term scaling-condition-header");
            const conditionText = this.conditions(branchCondition)
              .replaceAll("自身当前生命比例", "自身生命比例");
            header.textContent = `（${conditionText}）`;
            body.append(header);

            const matchingAttack = conditionKey(branch) === conditionKey(conditionalAttack.Value);
            const attackValue = matchingAttack
              ? addValues(normal.Attack, conditionalAttack.Value.Rate) : normal.Attack;
            const attackSoulValue = matchingAttack
              ? addValues(soul?.Attack, attackSoul?.Rate) : soul?.Attack;
            if (present(attackValue)) {
              const attackTerm = element("div", "scaling-term");
              attackTerm.append(this.pair(attackValue, attackSoulValue, true));
              attackTerm.append(document.createTextNode(` ${this.property({ Property: "Attack" })}`));
              body.append(attackTerm);
            }
            const branchValue = { ...conditionalConst.Value, ...branch, Who: "Source", __displaySelf: true };
            delete branchValue.ConditionRates;
            delete branchValue.Condition;
            const soulBranch = constSoul?.ConditionRates?.[index]
              ? { ...constSoul, ...constSoul.ConditionRates[index], Who: "Source" } : undefined;
            if (soulBranch) {
              delete soulBranch.ConditionRates;
              delete soulBranch.Condition;
              delete soulBranch.Conditions;
            }
            const propertyTerm = element("div", "scaling-term");
            propertyTerm.append(this.expression(branchValue, soulBranch, "ConstAttack", false, false));
            body.append(propertyTerm);
          });
        } else {
        const formula = element("div", "scaling-term");
        if (hasAttack) {
          formula.append(this.pair(normal.Attack, soul?.Attack, true));
          formula.append(document.createTextNode(` ${this.property({ Property: "Attack" })}`));
        }
        // A linked skill still requires its own base-coefficient conditions;
        // the compact module only omits the surrounding invocation details.
        for (const effect of basicEffects.filter(plainTerm)) {
          const term = this.expression(effect.Value, soulEffects.get(`${effect.Key}/${effect.Kind}`)?.Value, effect.Kind, true, true);
          this.appendTerm(formula, term, formula.childNodes.length > 0, true, false);
        }
        if (formula.childNodes.length) body.append(formula);
        for (const effect of basicEffects.filter((effect) => !plainTerm(effect))) {
          this.effect(body, effect, soulEffects.get(`${effect.Key}/${effect.Kind}`), true, displayedSkill);
        }
        }
      }
      if (extraEffects.length) {
        const body = this.group(container, this.label("extra"));
        body.title = this.label("conditionHint");
        for (const effect of extraEffects) this.effect(body, effect, soulEffects.get(`${effect.Key}/${effect.Kind}`), showConditions, displayedSkill);
      }
      const criticalEffects = effects.filter((effect) => effect.Kind === "CriticalDamageBonus");
      const soulOnlyCritical = soulOnlyEffects.filter((effect) => effect.Kind === "CriticalDamageBonus");
      if (criticalEffects.length || soulOnlyCritical.length) {
        const body = this.group(container, this.label("CriticalDamageBonus"));
        for (const effect of criticalEffects) this.effect(body, effect, soulEffects.get(`${effect.Key}/${effect.Kind}`), showConditions, displayedSkill);
        for (const effect of soulOnlyCritical) {
          const value = this.effect(body, effect, undefined, showConditions, displayedSkill);
          value.classList.add("scaling-soul");
          value.title = this.label("onlySoul");
          value.prepend(document.createTextNode("("));
          value.append(document.createTextNode(")"));
        }
      }
      if ((hasAttack || basicEffects.length) && present(normal.Power)) {
        const body = this.group(container, this.label("power"));
        body.title = this.label("powerHint");
        body.append(this.pair(normal.Power, soul?.Power), document.createTextNode("×1.871"));
      }
      const effectiveLinkedUpgrades = [
        ...replacementUpgradeEffects.filter((effect) => effect.Value?.Condition?.CheckID === skill.ID),
        ...counterUpgradeEffects,
        ...additionUpgradeEffects.filter((effect) => additionUpgradeTarget(effect) === skill.ID),
      ];
      // A non-attacking passive can own the level table inherited by its
      // addition skill. Show that scalar upgrade only on the actual attack.
      // Require one complete, unvisited target and identical level provenance;
      // keep unresolved, mixed, and soul variants on the ordinary path.
      const inheritedAdditionUpgrade = (() => {
        if (skill.SkillType !== "Passive" || skill.Scaling?.LevelSource !== skill.ID
            || skill.Scaling?.Soul || soul || present(normal.Attack) || present(normal.Power)
            || effects.length || skill.Scaling?.Warnings?.length) return false;
        const upgrade = normal.UpgradeDamage;
        if (!Number.isFinite(upgrade?.Max) || upgrade.Max <= 0
            || stableKey(upgrade) !== stableKey({ Min: 0, Max: upgrade.Max })
            || !Number.isInteger(skill.Scaling.MaxLevel) || skill.Scaling.MaxLevel < 2) return false;
        const references = normal.References || [];
        const targetID = references[0]?.SkillID;
        if (!targetID || visited.has(targetID)
            || !references.every((reference) => reference.Kind === "AdditionSkill"
              && reference.SkillID === targetID)) return false;
        const linked = this.skills.get(targetID);
        const scaling = linked?.Scaling;
        return !!linked && !linked.missing && linked.SkillType !== "Passive"
          && !scaling?.Soul && !scaling?.Warnings?.length
          && scaling?.LevelSource === skill.ID && scaling.MaxLevel === skill.Scaling.MaxLevel
          && magnitude(scaling.Normal?.Attack) && !(scaling.Normal?.References || []).length
          && stableKey(scaling.Normal?.UpgradeDamage) === stableKey(upgrade);
      })();
      if (!inheritedAdditionUpgrade && (magnitude(normal.UpgradeDamage) || magnitude(soul?.UpgradeDamage))) {
        const body = this.group(container, this.label("upgrade"));
        body.title = this.label("upgradeHint");
        body.append(this.pair(normal.UpgradeDamage?.Max ?? normal.UpgradeDamage, soul?.UpgradeDamage?.Max ?? soul?.UpgradeDamage, true));
        // Only distinguish inherited values when this skill shows multiple
        // upgrade rows; a single inherited row keeps the compact wording.
        if (effectiveLinkedUpgrades.length && skill.Scaling?.LevelSource
            && skill.Scaling.LevelSource !== skill.ID) {
          body.append(document.createTextNode(`（${this.label("inherited")}）`));
        }
      }
      for (const effect of effectiveLinkedUpgrades) {
        const body = this.group(container, this.label("upgrade"));
        body.title = this.label("conditionHint");
        const soulEffect = soulEffects.get(`${effect.Key}/${effect.Kind}`);
        const value = effect.Value?.Rate;
        const soulValue = soulEffect?.Value?.Rate;
        body.append(this.pair(value?.Max ?? value, soulValue?.Max ?? soulValue, true));
      }
      const propertyEffects = effects.filter((effect) => effect.Kind === "PropertyRate");
      if (propertyEffects.length) {
        const body = this.group(container, this.label("PropertyRate"), "scaling-group--property-rate");
        body.previousElementSibling.textContent = this.label("PropertyRate");
        for (const effect of propertyEffects) {
          this.effect(body, effect, soulEffects.get(`${effect.Key}/${effect.Kind}`), showConditions, displayedSkill);
        }
      }
      const penetration = compactPenetration(effects, skill.ID, soul);
      if (penetration) {
        const body = this.group(container, this.label("penetration"));
        if (penetration.propertyDifference) {
          const value = penetration.propertyDifference.Value;
          const [positive, negative] = value.Rate > 0 ? [value, value.ValueConvert] : [value.ValueConvert, value];
          body.append(element("div", "scaling-term",
            `${this.number(Math.abs(value.Rate), true)} ×（${this.property(positive)} - ${this.property(negative)}）（${this.label("limit")} ${this.number(value.FinalMaxValue, true)}）`));
        } else {
          const base = penetration.base.Value, difference = penetration.difference.Value;
          // User-approved compact form: combine the two additive contributions
          // and show their total cap. MustPositive remains on the raw delta.
          body.append(element("div", "scaling-term",
            `${this.number(base.Rate, true)} + ${this.number(difference.Rate, true)} ×（${this.property(difference)} - ${this.property(difference.ValueConvert)}）（${this.label("limit")} ${this.number(base.Rate + difference.FinalMaxValue, true)}）`));
        }
      }
      const penetrationTerms = penetration?.propertyDifference
        ? [penetration.propertyDifference] : [penetration?.base, penetration?.difference];
      const otherEffects = effects.filter((effect) => !["ConstAttack", "AttRate", "ConditionAttack", "CriticalDamageBonus", "PropertyRate"].includes(effect.Kind)
        && !penetrationTerms.includes(effect));
      const otherGroups = groupCastFixedContributions(
        groupActionHeals(groupHealContributions(mergeTargetEffects(otherEffects, soulEffects), soulEffects), soulEffects), soulEffects, skill.ID);
      for (const group of otherGroups) {
        const effect = group[0];
        let label = effect.Tags === "FixedDamage" ? this.label("fixedTag") : this.label(effect.Kind);
        const castFixedGroup = skill.ID === "H145S3" && group.length === 2 && stackCounterDisplay(group[1]);
        if (castFixedGroup) {
          label += `（${this.conditions(effect.Conditions)}）`;
        }
        const standalone = ["DirectHit", "FixedDamage"].includes(effect.Kind);
        const body = this.group(container, label, standalone ? "scaling-module" : "");
        if (isActionHealGroup(group, soulEffects)) {
          const burst = soulEffects.get(`${effect.Key}/${effect.Kind}`);
          const line = this.effect(body, { ...effect, Conditions: [] },
            burst ? { ...burst, Conditions: [] } : undefined, showConditions, displayedSkill);
          if (showConditions) line.append(element("span", "scaling-condition", `（${this.label("healActionTypes")}）`));
          continue;
        }
        if (castFixedGroup) body.parentElement.classList.add("scaling-group--branching");
        if (castFixedGroup) {
          const line = element("div", "scaling-term");
          group.forEach((contribution, index) => {
            const temporary = document.createElement("div");
            this.effect(temporary, contribution, soulEffects.get(`${contribution.Key}/${contribution.Kind}`), false, displayedSkill);
            const rendered = temporary.firstElementChild;
            if (index) line.append(document.createTextNode(" + "));
            while (rendered?.firstChild) line.append(rendered.firstChild);
          });
          body.append(line);
          continue;
        }
        const branches = showConditions && group.length === 1
          ? sharedHealBranches(effect, soulEffects.get(`${effect.Key}/${effect.Kind}`)) : null;
        if (branches) {
          body.parentElement.classList.add("scaling-group--branching");
          for (const branch of branches) {
            body.append(element("div", "scaling-term scaling-condition-header", `（${this.conditions([branch.condition])}）`));
            for (const value of branch.values) {
              const term = element("div", "scaling-term");
              term.append(this.expression(value, undefined, effect.Kind));
              body.append(term);
            }
          }
          continue;
        }
        for (const contribution of group) {
          this.effect(body, contribution, soulEffects.get(`${contribution.Key}/${contribution.Kind}`), showConditions && !standalone, displayedSkill);
        }
      }
      for (const effect of mergeTargetEffects(soulOnlyEffects.filter((effect) => effect.Kind !== "CriticalDamageBonus"))) {
        const label = effect.Tags === "FixedDamage" ? this.label("fixedTag") : this.label(effect.Kind);
        const body = this.group(container, label);
        const value = element("span", "scaling-soul", "(");
        value.title = this.label("onlySoul");
        value.append(this.expression(effect.Value, undefined, effect.Kind, showConditions && !["DirectHit", "FixedDamage"].includes(effect.Kind)), document.createTextNode(")"));
        body.append(value);
      }
      const refs = [...(normal.References || [])];
      for (const reference of soul?.References || []) {
        if (!refs.some((item) => item.SkillID === reference.SkillID && item.Kind === reference.Kind)) refs.push({ ...reference, SoulOnly: true });
      }
      const renderedRefs = new Set();
      for (const reference of refs) {
        const refKey = `${reference.Kind}/${reference.SkillID}`;
        if (visited.has(reference.SkillID) || renderedRefs.has(refKey)) continue;
        renderedRefs.add(refKey);
        const linked = this.skills.get(reference.SkillID);
        const linkedSoul = this.linkedSoul(reference, linked, skill);
        if (linked && !linked.missing && !this.hasValues(linked, linkedSoul, visited)) continue;
        const section = element("div", `scaling-linked scaling-module${reference.SoulOnly ? " scaling-module--soul" : ""}`);
        section.append(element("h4", "scaling-module-title", `${this.label(reference.Kind)} · ${linked?.NAME || reference.SkillID}`));
        if (reference.SoulOnly) section.title = this.label("onlySoul");
        if (linked?.Scaling?.Normal) {
          this.mode(section, linked, linked.Scaling.Normal, linkedSoul, new Set([...visited, reference.SkillID]), false);
        } else section.append(element("p", "scaling-warning", this.label("missingSkill")));
        container.append(section);
      }
    }

    render(skill) {
      if (!skill.Scaling?.Normal) return null;
      const section = element("div", "character-skill__scaling");
      section.append(element("h3", "", this.label("title")));
      this.mode(section, skill, skill.Scaling.Normal, skill.Scaling.Soul, new Set([skill.ID]));
      if (section.children.length === 1 && !skill.Scaling.Warnings?.length) return null;
      if (skill.Scaling.Warnings?.length) {
        const details = element("details", "scaling-warning");
        details.append(element("summary", "", this.label("warning")));
        for (const warning of skill.Scaling.Warnings) details.append(element("p", "", `${this.label(warning.Code)} · ${warning.EffectID || warning.Field || warning.SkillID}`));
        section.append(details);
      }
      return section;
    }
  }

  window.renderArchiveScaling = (skill, data, translations) => new ScalingView(data, translations).render(skill);
})();

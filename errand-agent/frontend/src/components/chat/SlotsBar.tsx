import React from 'react';
import { IntentSlots } from '../../types';
import { Tag, AlertCircle, Wrench, MapPin, Calendar, Clock, DollarSign, UserCheck } from 'lucide-react';

interface SlotsBarProps {
  slots: IntentSlots;
  missingFields: string[];
  turnCounter: number;
}

const formatService = (val: string | null): string => {
  if (!val) return '';
  const lower = val.toLowerCase();
  if (lower.includes('plumb')) return 'Plumbing';
  if (lower.includes('elec')) return 'Electrical';
  if (lower.includes('ac')) return 'AC Servicing';
  return val.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
};

const formatDate = (val: string | null): string => {
  if (!val) return '';
  if (val === 'this_week') return 'This Week';
  if (val.startsWith('Saturday')) return 'Sat (Nov 14)';
  if (val.startsWith('Sunday')) return 'Sun (Nov 15)';
  return val.replace(/_/g, ' ');
};

const formatTime = (val: string | null): string => {
  if (!val) return '';
  if (val.includes('morning')) return 'Morning (08:00–12:00)';
  if (val.includes('afternoon')) return 'Afternoon (12:00–17:00)';
  if (val.includes('evening')) return 'Evening (17:00–21:00)';
  if (val.includes('after 10')) return 'After 10:00 AM';
  return val;
};

export const SlotsBar: React.FC<SlotsBarProps> = ({ slots, missingFields, turnCounter }) => {
  const safeSlots = slots || ({} as IntentSlots);
  const safeMissing = missingFields || [];

  const slotItems = [
    {
      key: 'service',
      label: 'Service',
      value: formatService(safeSlots.service),
      chipClass: 'border-sky-500/30 bg-sky-950/40 text-sky-200',
      icon: <Wrench className="w-3 h-3 text-sky-400" />,
    },
    {
      key: 'area',
      label: 'Area',
      value: safeSlots.area,
      chipClass: 'border-emerald-500/30 bg-emerald-950/40 text-emerald-200',
      icon: <MapPin className="w-3 h-3 text-emerald-400" />,
    },
    {
      key: 'date_window',
      label: 'Date',
      value: formatDate(safeSlots.date_window),
      chipClass: 'border-indigo-500/30 bg-indigo-950/40 text-indigo-200',
      icon: <Calendar className="w-3 h-3 text-indigo-400" />,
    },
    {
      key: 'time_window',
      label: 'Time',
      value: formatTime(safeSlots.time_window),
      chipClass: 'border-cyan-500/30 bg-cyan-950/40 text-cyan-200',
      icon: <Clock className="w-3 h-3 text-cyan-400" />,
    },
    {
      key: 'max_budget',
      label: 'Budget',
      value: safeSlots.max_budget ? `₹${safeSlots.max_budget.toLocaleString()}` : null,
      chipClass: 'border-amber-500/30 bg-amber-950/40 text-amber-200',
      icon: <DollarSign className="w-3 h-3 text-amber-400" />,
    },
    {
      key: 'preferred_provider',
      label: 'Provider',
      value: safeSlots.preferred_provider_id,
      chipClass: 'border-purple-500/30 bg-purple-950/40 text-purple-200',
      icon: <UserCheck className="w-3 h-3 text-purple-400" />,
    },
  ];

  const activeSlots = slotItems.filter((item) => Boolean(item.value));

  return (
    <div className="px-4 py-2 bg-zinc-900/80 border-b border-zinc-800/80 flex items-center justify-between text-xs shrink-0 select-none backdrop-blur-md">
      <div className="flex items-center gap-2 overflow-hidden flex-1">
        <span className="text-[10px] font-bold text-zinc-400 uppercase tracking-wider shrink-0 flex items-center gap-1 font-mono">
          <Tag className="w-3 h-3 text-sky-400" />
          <span>Intent Slots:</span>
        </span>

        {activeSlots.length === 0 ? (
          <span className="text-zinc-500 italic text-[11px]">
            No slots extracted yet. Prompt the agent with an errand request.
          </span>
        ) : (
          <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar py-0.5">
            {activeSlots.map((item) => (
              <div
                key={item.key}
                className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full border text-[11px] font-medium shrink-0 shadow-sm transition hover:brightness-110 ${item.chipClass}`}
              >
                {item.icon}
                <span className="opacity-70 text-[10px]">{item.label}:</span>
                <span className="font-semibold text-white">{item.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Right: Missing Fields Pill & Turn Counter */}
      <div className="flex items-center gap-2 shrink-0 pl-3">
        {safeMissing.length > 0 && safeSlots.service && (
          <div className="hidden lg:flex items-center gap-1.5 px-2.5 py-0.5 rounded-full bg-amber-950/50 text-amber-300 border border-amber-600/50 text-[10px] font-mono shadow-sm">
            <AlertCircle className="w-3 h-3 text-amber-400" />
            <span>Missing: {safeMissing.join(', ')}</span>
          </div>
        )}
        <span className="text-[11px] font-mono font-semibold text-zinc-300 bg-zinc-950/80 px-2.5 py-0.5 rounded-full border border-zinc-700/80 shadow-sm">
          Turn #{turnCounter}
        </span>
      </div>
    </div>
  );
};

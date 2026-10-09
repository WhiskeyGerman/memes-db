export enum MediaKind { Image = "image", Animation = "animation", Video = "video", Audio = "audio" }
export enum FeatureState { Pending = "pending", Available = "available", Failed = "failed" }
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
export const PAGE_SIZE = 24;
export const FILE_ACCEPT = ".jpg,.jpeg,.png,.webp,.avif,.gif,.apng,.mp4,.webm,.mp3,.m4a,.wav,.ogg,.flac";

export interface IUser { user_id: string; login: string }
export interface IMedia {
  media_id: string; group_id: string; composition_revision: number; membership_id: string;
  published_at: string; media_kind: MediaKind; format: string; url: string;
  author: { user_id: string | null; display_name: string };
  features: { kind: string; state: string; url?: string }[];
}
export interface IPage { items: IMedia[]; next: string | null; degraded?: boolean }
export interface IGroup { group_id: string; composition_revision: number; member_count: number; items: IMedia[] }
export interface IVote { ballot_id: string; active_votes: number; threshold: number; applied: boolean }
export interface IUploadResult { media_id: string; duplicate: boolean }

/** Прикладная граница браузерного клиента. Доменные решения остаются в backend. */
export abstract class IBackend {
  abstract restore(): Promise<IUser | null>;
  abstract signIn(login: string, password: string, register: boolean): Promise<IUser>;
  abstract signOut(): Promise<void>;
  abstract feed(next?: string, signal?: AbortSignal): Promise<IPage>;
  abstract search(text: string, next?: string, signal?: AbortSignal): Promise<IPage>;
  abstract group(id: string, offset?: number, signal?: AbortSignal): Promise<IGroup>;
  abstract media(id: string, signal?: AbortSignal): Promise<IMedia>;
  abstract upload(file: File): Promise<IUploadResult>;
  abstract detach(membership: string): Promise<IVote>;
  abstract withdraw(ballot: string): Promise<IVote>;
  abstract remove(id: string): Promise<void>;
}
